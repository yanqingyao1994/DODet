import torch
import torch.nn as nn
from mmcv.cnn import ConvModule

from mmdet.models.builder import HEADS
from .obbox_head import OBBoxHead
from mmdet.models.roi_heads.roi_extractors.obb.obb_single_level_roi_extractor import OBBSingleRoIExtractor

@HEADS.register_module()
class OBBConvFCBBoxHeadRefine(OBBoxHead):
    r"""More general bbox head, with shared conv and fc layers and two optional
    separated branches.

    .. code-block:: none

                                    /-> cls convs -> cls fcs -> cls
        shared convs -> shared fcs
                                    \-> reg convs -> reg fcs -> reg
    """  # noqa: W605

    def __init__(self,
                 num_shared_convs=0,
                 num_shared_fcs=0,
                 num_cls_convs=0,
                 num_cls_fcs=0,
                 num_reg_convs=0,
                 num_reg_fcs=0,
                 conv_out_channels=256,
                 fc_out_channels=1024,
                 conv_cfg=None,
                 norm_cfg=None,
                 *args,
                 **kwargs):
        super(OBBConvFCBBoxHeadRefine, self).__init__(*args, **kwargs)
        # assert (num_shared_convs + num_shared_fcs + num_cls_convs +
        #         num_cls_fcs + num_reg_convs + num_reg_fcs > 0)
        if num_cls_convs > 0 or num_reg_convs > 0:
            assert num_shared_fcs == 0
        if not self.with_cls:
            assert num_cls_convs == 0 and num_cls_fcs == 0
        if not self.with_reg:
            assert num_reg_convs == 0 and num_reg_fcs == 0
        self.num_shared_convs = num_shared_convs
        self.num_shared_fcs = num_shared_fcs
        self.num_cls_convs = num_cls_convs
        self.num_cls_fcs = num_cls_fcs
        self.num_reg_convs = num_reg_convs
        self.num_reg_fcs = num_reg_fcs
        self.conv_out_channels = conv_out_channels
        self.fc_out_channels = fc_out_channels
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg

        # add shared convs and fcs
        self.shared_convs, self.shared_fcs, last_layer_dim = \
            self._add_conv_fc_branch(
                self.num_shared_convs, self.num_shared_fcs, self.in_channels,
                True)
        self.shared_out_channels = last_layer_dim

        # add cls specific branch
        self.cls_convs, self.cls_fcs, self.cls_last_dim = \
            self._add_conv_fc_branch(
                self.num_cls_convs, self.num_cls_fcs, self.shared_out_channels)

        # add reg specific branch
        self.reg_convs, self.reg_fcs, self.reg_last_dim = \
            self._add_conv_fc_branch(
                self.num_reg_convs, self.num_reg_fcs, self.shared_out_channels)

        if self.num_shared_fcs == 0 and not self.with_avg_pool:
            if self.num_cls_fcs == 0:
                self.cls_last_dim *= self.roi_feat_area
            if self.num_reg_fcs == 0:
                self.reg_last_dim *= self.roi_feat_area

        self.relu = nn.ReLU(inplace=True)
        # reconstruct fc_cls and fc_reg since input channels are changed
        if self.with_cls:
            self.fc_cls = nn.Sequential(
                nn.Flatten(1), nn.Linear(self.in_channels*self.roi_feat_area, 1024), nn.ReLU(True),
                nn.Linear(1024, 1024), nn.ReLU(True), nn.Linear(1024, self.num_classes + 1)
            )
        if self.with_reg:
            out_dim_reg = self.reg_dim if self.reg_class_agnostic else \
                    self.reg_dim * self.num_classes
            self.fc_reg = nn.Sequential(
                nn.Conv2d(self.in_channels, 1024, 3, bias=False), nn.BatchNorm2d(1024), nn.ReLU(True),
                nn.Conv2d(1024, 1024, 3, bias=False), nn.BatchNorm2d(1024), nn.ReLU(True),
                nn.AvgPool2d(3), nn.Flatten(1), nn.Linear(1024, out_dim_reg)
            )
        self.roiobb = OBBSingleRoIExtractor(
            roi_layer=dict(
            type='OBBDeformRoIPoolingPack',
            out_size=7,
            out_channels=256,
            no_trans=False,
            group_size=1,
            trans_std=0.1),
            out_channels = 256,
            featmap_strides = [4, 8, 16, 32],
            extend_factor=(1.4, 1.2))

    def _add_conv_fc_branch(self,
                            num_branch_convs,
                            num_branch_fcs,
                            in_channels,
                            is_shared=False):
        """Add shared or separable branch

        convs -> avg pool (optional) -> fcs
        """
        last_layer_dim = in_channels
        # add branch specific conv layers
        branch_convs = nn.ModuleList()
        if num_branch_convs > 0:
            for i in range(num_branch_convs):
                conv_in_channels = (
                    last_layer_dim if i == 0 else self.conv_out_channels)
                branch_convs.append(
                    ConvModule(
                        conv_in_channels,
                        self.conv_out_channels,
                        3,
                        padding=1,
                        conv_cfg=self.conv_cfg,
                        norm_cfg=self.norm_cfg))
            last_layer_dim = self.conv_out_channels
        # add branch specific fc layers
        branch_fcs = nn.ModuleList()
        if num_branch_fcs > 0:
            # for shared branch, only consider self.with_avg_pool
            # for separated branches, also consider self.num_shared_fcs
            if (is_shared
                    or self.num_shared_fcs == 0) and not self.with_avg_pool:
                last_layer_dim *= self.roi_feat_area
            for i in range(num_branch_fcs):
                fc_in_channels = (
                    last_layer_dim if i == 0 else self.fc_out_channels)
                branch_fcs.append(
                    nn.Linear(fc_in_channels, self.fc_out_channels))
            last_layer_dim = self.fc_out_channels
        return branch_convs, branch_fcs, last_layer_dim

    def init_weights(self):
        pass

    def forward(self, x):
        # roi extractor more
        x, feats, rois = x

        # shared part
        if self.num_shared_convs > 0:
            for conv in self.shared_convs:
                x = conv(x)

        if self.num_shared_fcs > 0:
            if self.with_avg_pool:
                x = self.avg_pool(x)

            x = x.flatten(1)

            for fc in self.shared_fcs:
                x = self.relu(fc(x))
        # separate branches
        x_cls = x
        x_reg = x

        for conv in self.cls_convs:
            x_cls = conv(x_cls)
        if x_cls.dim() > 2:
            if self.with_avg_pool:
                x_cls = self.avg_pool(x_cls)
            x_cls = x_cls.flatten(1)
        for fc in self.cls_fcs:
            x_cls = self.relu(fc(x_cls))

        for conv in self.reg_convs:
            x_reg = conv(x_reg)
        if x_reg.dim() > 2:
            if self.with_avg_pool:
                x_reg = self.avg_pool(x_reg)
            x_reg = x_reg.flatten(1)
        for fc in self.reg_fcs:
            x_reg = self.relu(fc(x_reg))


        # our implementation
        # 1. regression bboxes
        bbox_pred = self.fc_reg(x) if self.with_reg else None
        # 2. bbox roi refine
        bbox = self.bbox_coder.decode(rois[:, 1:], bbox_pred.detach())
        roisx = torch.empty(rois.size(0), 6).to(bbox.device)
        roisx[:, 0] = rois[:, 0]
        roisx[:, 1:] = bbox
        x = self.roiobb(feats, roisx)
        # 3. classification branch
        cls_score = self.fc_cls(x) if self.with_cls else None


        return cls_score, bbox_pred


@HEADS.register_module()
class OBBBBoxHeadRefine(OBBConvFCBBoxHeadRefine):

    def __init__(self, fc_out_channels=1024, *args, **kwargs):
        super(OBBBBoxHeadRefine, self).__init__(
            num_shared_convs=0,
            num_shared_fcs=0,
            num_cls_convs=0,
            num_cls_fcs=0,
            num_reg_convs=0,
            num_reg_fcs=0,
            fc_out_channels=fc_out_channels,
            *args,
            **kwargs)
