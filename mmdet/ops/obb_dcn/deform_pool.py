import torch
import torch.nn as nn
from torch.autograd import Function
from torch.autograd.function import once_differentiable
from torch.nn.modules.utils import _pair

from . import obb_deform_pool_ext
import torch.nn as nn
from collections import OrderedDict
import torch.nn.functional as F
import math


COEFF = 12.0

class SoftGate(nn.Module):
    def __init__(self):
        super(SoftGate, self).__init__()

    def forward(self, x):
        return torch.sigmoid(x).mul(COEFF)

class AWPool(nn.Module):
    def __init__(self, channels):
        super(AWPool, self).__init__()

        self.weight = nn.Sequential(
            OrderedDict((
                ('conv', nn.Conv2d(channels, channels, 3, padding=1, bias=False)),
                ('bn', nn.InstanceNorm2d(channels, affine=True)),
                ('gate', SoftGate()),
            ))
        )

    def init_layer(self):
        self.weight[0].weight.data.fill_(0.0)

    def forward(self, x, kernel=3, stride=2, padding=1):
        weight = self.weight(x).exp()
        return F.avg_pool2d(x * weight, kernel, stride, padding) / F.avg_pool2d(weight, kernel, stride, padding)


class DeformRoIPoolingFunction(Function):

    @staticmethod
    def forward(ctx,
                data,
                rois,
                offset,
                spatial_scale,
                out_size,
                out_channels,
                no_trans,
                group_size=1,
                part_size=None,
                sample_per_part=4,
                trans_std=.0):
        # TODO: support unsquare RoIs
        out_h, out_w = _pair(out_size)
        assert isinstance(out_h, int) and isinstance(out_w, int)
        assert out_h == out_w
        out_size = out_h  # out_h and out_w must be equal

        ctx.spatial_scale = spatial_scale
        ctx.out_size = out_size
        ctx.out_channels = out_channels
        ctx.no_trans = no_trans
        ctx.group_size = group_size
        ctx.part_size = out_size if part_size is None else part_size
        ctx.sample_per_part = sample_per_part
        ctx.trans_std = trans_std

        assert 0.0 <= ctx.trans_std <= 1.0
        if not data.is_cuda:
            raise NotImplementedError

        n = rois.shape[0]
        output = data.new_empty(n, out_channels, out_size, out_size)
        output_count = data.new_empty(n, out_channels, out_size, out_size)
        obb_deform_pool_ext.deform_psroi_pooling_forward(
            data, rois, offset, output, output_count, ctx.no_trans,
            ctx.spatial_scale, ctx.out_channels, ctx.group_size, ctx.out_size,
            ctx.part_size, ctx.sample_per_part, ctx.trans_std)

        if data.requires_grad or rois.requires_grad or offset.requires_grad:
            ctx.save_for_backward(data, rois, offset)
        ctx.output_count = output_count

        return output

    @staticmethod
    @once_differentiable
    def backward(ctx, grad_output):
        if not grad_output.is_cuda:
            raise NotImplementedError

        data, rois, offset = ctx.saved_tensors
        output_count = ctx.output_count
        grad_input = torch.zeros_like(data)
        grad_rois = None
        grad_offset = torch.zeros_like(offset)

        obb_deform_pool_ext.deform_psroi_pooling_backward(
            grad_output, data, rois, offset, output_count, grad_input,
            grad_offset, ctx.no_trans, ctx.spatial_scale, ctx.out_channels,
            ctx.group_size, ctx.out_size, ctx.part_size, ctx.sample_per_part,
            ctx.trans_std)
        return (grad_input, grad_rois, grad_offset, None, None, None, None,
                None, None, None, None)


deform_roi_pooling = DeformRoIPoolingFunction.apply


class OBBDeformRoIPooling(nn.Module):

    def __init__(self,
                 spatial_scale,
                 out_size,
                 out_channels,
                 no_trans,
                 group_size=1,
                 part_size=None,
                 sample_per_part=4,
                 trans_std=.0):
        super(OBBDeformRoIPooling, self).__init__()
        self.spatial_scale = spatial_scale
        self.out_size = _pair(out_size)
        self.out_channels = out_channels
        self.no_trans = no_trans
        self.group_size = group_size
        self.part_size = out_size if part_size is None else part_size
        self.sample_per_part = sample_per_part
        self.trans_std = trans_std

    def forward(self, data, rois, offset):
        if self.no_trans:
            offset = data.new_empty(0)
        return deform_roi_pooling(data, rois, offset, self.spatial_scale,
                                  self.out_size, self.out_channels,
                                  self.no_trans, self.group_size,
                                  self.part_size, self.sample_per_part,
                                  self.trans_std)


class OBBDeformRoIPoolingPack(OBBDeformRoIPooling):

    def __init__(self,
                 spatial_scale,
                 out_size,
                 out_channels,
                 no_trans,
                 group_size=1,
                 part_size=None,
                 sample_per_part=4,
                 trans_std=.0,
                 num_offset_fcs=3,
                 deform_fc_channels=1024):
        super(OBBDeformRoIPoolingPack,
              self).__init__(spatial_scale, out_size, out_channels, no_trans,
                             group_size, part_size, sample_per_part, trans_std)

        self.num_offset_fcs = num_offset_fcs
        self.deform_fc_channels = deform_fc_channels
        self.offset_size = (3,3)
        self.awp = AWPool(256)
        self.awp.init_layer()

        if not no_trans:
            seq = []
            ic = self.offset_size[0] * self.offset_size[1] * self.out_channels
            for i in range(self.num_offset_fcs):
                if i < self.num_offset_fcs - 1:
                    oc = self.deform_fc_channels
                else:
                    oc = self.out_size[0] * self.out_size[1] * 2
                seq.append(nn.Linear(ic, oc))
                ic = oc
                if i < self.num_offset_fcs - 1:
                    seq.append(nn.ReLU(inplace=True))
            self.offset_fc = nn.Sequential(*seq)
            self.offset_fc[-1].weight.data.zero_()
            self.offset_fc[-1].bias.data.zero_()

    def forward(self, data, rois):
        assert data.size(1) == self.out_channels
        n = rois.shape[0]
        if n == 0:
            return data.new_empty(n, self.out_channels, self.out_size[0],
                                  self.out_size[1])
        if self.no_trans:
            offset = data.new_empty(0)
            return deform_roi_pooling(data, rois, offset, self.spatial_scale,
                                      self.offset_size, self.out_channels,
                                      self.no_trans, self.group_size,
                                      self.offset_size, self.sample_per_part,
                                      self.trans_std)
        else:
            offset = data.new_empty(0)
            x = deform_roi_pooling(data, rois, offset, self.spatial_scale,
                                   self.offset_size, self.out_channels, True,
                                   self.group_size, self.offset_size[0],
                                   self.sample_per_part, self.trans_std)
            offset = self.offset_fc(x.view(n, -1))
            offset = offset.view(n, 2, self.out_size[0], self.out_size[1])
            offset = F.interpolate(offset, scale_factor=2, mode='nearest')

            roi_feats = deform_roi_pooling(data, rois, offset, self.spatial_scale,
                                      (self.out_size[0]*2,self.out_size[1]*2), self.out_channels,
                                      self.no_trans, self.group_size,
                                      self.part_size*2, self.sample_per_part,
                                      self.trans_std)
            roi_feats = self.awp(roi_feats)
            return roi_feats


class OBBModulatedDeformRoIPoolingPack(OBBDeformRoIPooling):

    def __init__(self,
                 spatial_scale,
                 out_size,
                 out_channels,
                 no_trans,
                 group_size=1,
                 part_size=None,
                 sample_per_part=4,
                 trans_std=.0,
                 num_offset_fcs=3,
                 num_mask_fcs=2,
                 deform_fc_channels=1024):
        super(OBBModulatedDeformRoIPoolingPack,
              self).__init__(spatial_scale, out_size, out_channels, no_trans,
                             group_size, part_size, sample_per_part, trans_std)

        self.num_offset_fcs = num_offset_fcs
        self.num_mask_fcs = num_mask_fcs
        self.deform_fc_channels = deform_fc_channels

        if not no_trans:
            offset_fc_seq = []
            ic = self.out_size[0] * self.out_size[1] * self.out_channels
            for i in range(self.num_offset_fcs):
                if i < self.num_offset_fcs - 1:
                    oc = self.deform_fc_channels
                else:
                    oc = self.out_size[0] * self.out_size[1] * 2
                offset_fc_seq.append(nn.Linear(ic, oc))
                ic = oc
                if i < self.num_offset_fcs - 1:
                    offset_fc_seq.append(nn.ReLU(inplace=True))
            self.offset_fc = nn.Sequential(*offset_fc_seq)
            self.offset_fc[-1].weight.data.zero_()
            self.offset_fc[-1].bias.data.zero_()

            mask_fc_seq = []
            ic = self.out_size[0] * self.out_size[1] * self.out_channels
            for i in range(self.num_mask_fcs):
                if i < self.num_mask_fcs - 1:
                    oc = self.deform_fc_channels
                else:
                    oc = self.out_size[0] * self.out_size[1]
                mask_fc_seq.append(nn.Linear(ic, oc))
                ic = oc
                if i < self.num_mask_fcs - 1:
                    mask_fc_seq.append(nn.ReLU(inplace=True))
                else:
                    mask_fc_seq.append(nn.Sigmoid())
            self.mask_fc = nn.Sequential(*mask_fc_seq)
            self.mask_fc[-2].weight.data.zero_()
            self.mask_fc[-2].bias.data.zero_()

    def forward(self, data, rois):
        assert data.size(1) == self.out_channels
        n = rois.shape[0]
        if n == 0:
            return data.new_empty(n, self.out_channels, self.out_size[0],
                                  self.out_size[1])
        if self.no_trans:
            offset = data.new_empty(0)
            return deform_roi_pooling(data, rois, offset, self.spatial_scale,
                                      self.out_size, self.out_channels,
                                      self.no_trans, self.group_size,
                                      self.part_size, self.sample_per_part,
                                      self.trans_std)
        else:
            offset = data.new_empty(0)
            x = deform_roi_pooling(data, rois, offset, self.spatial_scale,
                                   self.out_size, self.out_channels, True,
                                   self.group_size, self.part_size,
                                   self.sample_per_part, self.trans_std)
            offset = self.offset_fc(x.view(n, -1))
            offset = offset.view(n, 2, self.out_size[0], self.out_size[1])
            mask = self.mask_fc(x.view(n, -1))
            mask = mask.view(n, 1, self.out_size[0], self.out_size[1])
            return deform_roi_pooling(
                data, rois, offset, self.spatial_scale, self.out_size,
                self.out_channels, self.no_trans, self.group_size,
                self.part_size, self.sample_per_part, self.trans_std) * mask
