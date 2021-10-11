from multiprocessing import Pool
import os.path as osp
import BboxToolkit as bt


# img_dir = '/disk2/xiexingxing/wjb/split_dota/ss/test/images'
# ann_dir = '/disk2/xiexingxing/wjb/vis_result'
# save_dir = '/disk2/xiexingxing/wjb/vis/aopg_ms_05_ss'

img_dir = '//disk2/evannnnnnn/Datasets/DOTA/test/images'
ann_dir = '/home/evannnnnnn/OBBmmdetection2/results_OPG/results_our2_e2'
save_dir = '/home/evannnnnnn/OBBmmdetection2/vis_OPG/ours2_e2'
colors = [(0,128,255),(116,115,147),(255,0,0),(0,255,0),
          (42,42,165), (255,0,255),(205,250,255),(193,193,255),
          (0,0,255),(226,43,138), (107,183,189), (255,255,0),
          (139,139,0),(153,51,0),(0,255,255) ] 

infos, classes = bt.load_dota_submission(ann_dir)
colors = colors[:len(classes)]

# img_dir = '/disk2/xiexingxing/wjb/HRSC2016/FullDataSet/AllImages/'
# ann_dir = '/disk2/xiexingxing/wjb/results/sp_orpn/hrsc.pkl'
# save_dir = '/disk2/xiexingxing/wjb/vis/hrsc'
# infos, classes = bt.load_pkl(ann_dir)
# colors = 'green'

def vis(info):
    print(info['id'])
    bboxes = info['ann']['bboxes']
    labels = info['ann']['labels']
    scores = info['ann']['scores']

    bboxes = bboxes[scores > 0.3]
    labels = labels[scores > 0.3]
    scores = scores[scores > 0.3]
    if len(bboxes) == 0:
        return 
    bboxes = [bboxes[labels == i] for i in range(len(classes))]

    bt.imshow_bboxes(
        osp.join(img_dir, info['id'] + '.png'),
        bboxes,
        colors=colors,
        show=False,
        thickness=3,
        out_file=osp.join(save_dir, info['id'] + '.png')
    )

pool = Pool(5)
pool.map(vis, infos)
pool.close()


