# -*- coding:utf-8 -*-
"""
1,实现side refinement
2,不再均衡样本，所有的正负样本都参与分类计算
结论：所有的负样本参与，正负样本比例可能是200:20000，模型梯度会被负样本支配，因此不可以；
    若政正负样本是随机取出来的，这时候适合采用hard negtive方法，
    若所有样本都参与分类，适合采用facal loss，论文中说是单阶段检测模型适合用，比如yolo1的loss函数就是所有正负样本都在损失函数，而yolov2引入了anhcor，不知道是否有采样正负样本，
"""
import numpy as np
import numpy.random as npr
import tensorflow as tf
from lib.rpn_msr.generate_anchors import generate_anchors
from lib.utils.bbox import bbox_overlaps, bbox_intersections
from lib.fast_rcnn.config import cfg
from lib.fast_rcnn.bbox_transform import bbox_transform
import math

DEBUG = False
def split_frame(gt_boxes):
    gt_boxes = gt_boxes.astype(np.int32)
    list_box = list()
    for i in range(gt_boxes.shape[0]):
        list_box.append(gt_boxes[i][:])
    list_fine_box = list()
    for box in list_box:
        xmin, ymin, xmax, ymax = box[0], box[1], box[2], box[3]
        width = xmax - xmin
        height = ymax - ymin

        # reimplement
        step = 16.0
        x_left = []
        x_right = []
        x_left.append(xmin)
        x_left_start = int(math.ceil(xmin / 16.0) * 16.0)
        if x_left_start == xmin:
            x_left_start = xmin + 16
        for i in np.arange(x_left_start, xmax, 16):
            x_left.append(i)
        x_left = np.array(x_left)

        x_right.append(x_left_start - 1)
        for i in range(1, len(x_left) - 1):
            x_right.append(x_left[i] + 15)
        x_right.append(xmax)
        x_right = np.array(x_right)

        idx = np.where(x_left == x_right)
        x_left = np.delete(x_left, idx, axis=0)
        x_right = np.delete(x_right, idx, axis=0)

        for i in range(len(x_left)):
            list_fine_box.append([x_left[i], ymin, x_right[i], ymax, 1])

    gt_boxes = np.array(list_fine_box).astype(np.float32)
    return gt_boxes

def anchor_target_layer(rpn_cls_score, gt_boxes_large, gt_ishard, dontcare_areas, im_info, _feat_stride = [16,], anchor_scales = [16,]):
    """
    将gt_box划分为细框
    实现论文中的side-refinement
    arameters
    ----------
    rpn_cls_score: (1, H, W, Ax2) bg/fg scores of previous conv layer
    gt_boxes: (G, 5) vstack of [x1, y1, x2, y2, class]
    gt_ishard: (G, 1), 1 or 0 indicates difficult or not
    dontcare_areas: (D, 4), some areas may contains small objs but no labelling. D may be 0
    im_info: a list of [image_height, image_width, scale_ratios]
    _feat_stride: the downsampling ratio of feature map to the original input image
    anchor_scales: the scales to the basic_anchor (basic anchor is [16, 16])
    ----------
    :return:
    """
    gt_boxes = split_frame(gt_boxes_large)
    # gt_width = gt_boxes[:,2]-gt_boxes[:,0]
    _anchors = generate_anchors(scales=np.array(anchor_scales))  # 生成基本的anchor,一共9个
    _num_anchors = _anchors.shape[0]  # 9个anchor

    if DEBUG:
        print('anchors:')
        print(_anchors)
        print('anchor shapes:')
        print(np.hstack((
            _anchors[:, 2::4] - _anchors[:, 0::4],
            _anchors[:, 3::4] - _anchors[:, 1::4],
        )))
        _counts = cfg.EPS
        _sums = np.zeros((1, 4))
        _squared_sums = np.zeros((1, 4))
        _fg_sum = 0
        _bg_sum = 0
        _count = 0

    # allow boxes to sit over the edge by a small amount
    _allowed_border = 0

    im_info = im_info[0]  # 图像的高宽及通道数

    assert rpn_cls_score.shape[0] == 1, \
        'Only single item batches are supported'

    # map of shape (..., H, W)
    height, width = rpn_cls_score.shape[1:3]  # feature-map的高宽

    if DEBUG:
        print('AnchorTargetLayer: height', height, 'width', width)
        print('')
        print('im_size: ({}, {})'.format(im_info[0], im_info[1]))
        print('scale: {}'.format(im_info[2]))
        print('height, width: ({}, {})'.format(height, width))
        print('rpn: gt_boxes.shape', gt_boxes.shape)

    # 1. Generate proposals from bbox deltas and shifted anchors
    shift_x = np.arange(0, width) * _feat_stride  # (W)
    shift_y = np.arange(0, height) * _feat_stride  # (H)
    shift_x, shift_y = np.meshgrid(shift_x, shift_y)  # in W H order   # shift_x (H, W)  shift_y (H, W)

    # K is H x W
    shifts = np.vstack((shift_x.ravel(), shift_y.ravel(),
                        shift_x.ravel(),
                        shift_y.ravel())).transpose()  # 生成feature-map和真实image上anchor之间的偏移量     #(H*W, 4)
    # add A anchors (1, A, 4) to
    # cell K shifts (K, 1, 4) to get
    # shift anchors (K, A, 4)
    # reshape to (K*A, 4) shifted anchors
    A = _num_anchors  # 9个anchor
    K = shifts.shape[0]  # 50*37，feature-map的宽乘高的大小
    all_anchors = (_anchors.reshape((1, A, 4)) +
                   shifts.reshape((1, K, 4)).transpose((1, 0, 2)))  # 相当于复制宽高的维度，然后相加
    all_anchors = all_anchors.reshape((K * A, 4))
    total_anchors = int(K * A)

    # only keep anchors inside the image
    # 仅保留那些还在图像内部的anchor，超出图像的都删掉
    inds_inside = np.where(
        (all_anchors[:, 0] >= -_allowed_border) &
        (all_anchors[:, 1] >= -_allowed_border) &
        (all_anchors[:, 2] < im_info[1] + _allowed_border) &  # width
        (all_anchors[:, 3] < im_info[0] + _allowed_border)  # height
    )[0]

    if DEBUG:
        print('total_anchors', total_anchors)
        print('inds_inside', len(inds_inside))

    # keep only inside anchors
    anchors = all_anchors[inds_inside, :]  #保留那些在图像内的anchor   (In, 4)
    if DEBUG:
        print('anchors.shape', anchors.shape)

    #至此，anchor准备好了
    #--------------------------------------------------------------
    # label: 1 is positive, 0 is negative, -1 is dont care
    # (A)
    labels = np.empty((len(inds_inside), ), dtype=np.float32)
    labels.fill(0)#初始化label，均为-1

    # overlaps between the anchors and the gt boxes
    # overlaps (ex, gt), shape is A x G
    # 计算anchor和gt-box的overlap，用来给anchor上标签
    overlaps = bbox_overlaps(
        np.ascontiguousarray(anchors, dtype=np.float),
        np.ascontiguousarray(gt_boxes, dtype=np.float))  # 假设anchors有x个，gt_boxes有y个，返回的是一个（x,y）的数组
    # 存放每一个anchor和每一个gtbox之间的overlap
    argmax_overlaps = overlaps.argmax(axis=1)  # (A)#找到和每一个anchor，overlap最大的那个gt
    max_overlaps = overlaps[
        np.arange(len(inds_inside)), argmax_overlaps]  # 假如在内部的anchor有900个 ，(900,), 表示的是每一个anchor最大的overlaps值
    gt_argmax_overlaps = overlaps.argmax(axis=0)  # G#找到所有anchor中与gtbox，overlap最大的那个anchor  # (3)
    if DEBUG:
        print('获取所有anchor中与gt相交最大的哪几个anchor的索引')
        print('gt_argmax_overlaps.shape', gt_argmax_overlaps.shape)
    gt_max_overlaps = overlaps[gt_argmax_overlaps,
                               np.arange(
                                   overlaps.shape[1])]  # 比如有3个gt 那么就得到(3,),表示的是上一步找到的与gt的overlap最大的3个anchor的overlap值
    gt_argmax_overlaps = np.where(overlaps == gt_max_overlaps )[0]  # (3, ) 表示的是哪几个与gt有最大overlap的anchor的索引
    if DEBUG:
        print('这一步是找到那些同样与gt有最大overlap的索引，上一步找到的4个，这一步找到其他重复的')
        print('gt_argmax_overlaps.shape', gt_argmax_overlaps.shape)

    if not cfg.TRAIN.RPN_CLOBBER_POSITIVES:
        # assign bg labels first so that positive labels can clobber them
        labels[max_overlaps < cfg.TRAIN.RPN_NEGATIVE_OVERLAP] = 0#先给背景上标签，小于0.3overlap的

    # fg label: for each gt, anchor with highest overlap
    labels[gt_argmax_overlaps] = 1  #  每个位置上的9个anchor中overlap最大的认为是前景
    # fg label: above threshold IOU
    labels[max_overlaps >= cfg.TRAIN.RPN_POSITIVE_OVERLAP] = 1#overlap大于0.7的认为是前景



    if cfg.TRAIN.RPN_CLOBBER_POSITIVES:
        # assign bg labels last so that negative labels can clobber positives
        labels[max_overlaps < cfg.TRAIN.RPN_NEGATIVE_OVERLAP] = 0

    if DEBUG:
        print('在过滤数量之前：')
        print('正样本：',len(np.where(labels==1)[0]))
        print('负样本：', len(np.where(labels == 0)[0]))
        print('忽略样本：', len(np.where(labels == -1)[0]))


    # 不再限制正负样本的数量

    if DEBUG:
        print("考虑均衡住正负样本以后：")
        print('正样本：', len(np.where(labels == 1)[0]))
        print('负样本：', len(np.where(labels == 0)[0]))
        print('忽略样本：', len(np.where(labels == -1)[0]))
    # 至此， 上好标签，开始计算rpn-box的真值

    #
    v_target, o_target = _compute_targets(anchors, gt_boxes[argmax_overlaps, :])  #   根据anchor和gtbox计算得真值（anchor和gtbox之间的偏差）

    # 但是计算损失函数的时候，其实是需要j索引和k索引，所以计算好这两个索引，一并返回，帮助计算损失函数

    # j索引，有效索引：正锚点或者与gt的overlap大于0.5以上的锚点的索引

    # 正锚点
    positive_index = np.where(labels==1)[0]  # 应该是一个（p,）p应该不大于128

    #
    # ignore_index = np.where(labels==-1)[0]  # 应该是一个（n,）n应该很大，因为忽略的anchor很多
    keep_index = np.where(labels!=-1)[0]
    _ = np.where(max_overlaps>0.5)[0]   # 应该是一个（c,）,表示overlap大于0.5的anchor的索引

    remove_ignore = list()
    for i in range(_.shape[0]):
        if i in keep_index:
            remove_ignore.append(_[i])
    remove_ignore = np.array(remove_ignore)
    effect_index = np.append(positive_index, remove_ignore)

    remove_repeat = np.array(list(set(list(effect_index))))

    j_index = remove_repeat.astype(np.int32)

    j_index1 = np.zeros((len(inds_inside) ), dtype = np.int32)
    j_index1[j_index] = 1

    # k 索引 , 边缘索引

    # 先找到所有的可以认为是边缘的gt框,这里简单的认为是边缘框和左右各自一个。
    #ori_gt_box = (gt_boxes/im_info[2]).astype(np.int32, copy=False)
    ori_gt_box = gt_boxes.astype(np.float32, copy=False)
    # 找到左右边界框，矩阵操作实现  todo
    list_left_index = list()
    list_right_index = list()
    for i in range(ori_gt_box.shape[0]):
        if ori_gt_box[i][2] - ori_gt_box[i][0]!=15 :
            list_left_index.append(i)
        else:
            continue
    list_index1 = list_left_index+list_right_index
    # 去除不属于gt中的索引和重复的索引
    list_index2 = list(set(list_index1))
    list_index3 = sorted(list_index2)
    list_index4 = list()
    for index in list_index3:
        if index in range(ori_gt_box.shape[0]):
            list_index4.append(index)

    # if DEBUG:
    #     print("list_left_index", list_left_index)
    #     print("list_right_index", list_right_index)
    #     print("list_index1", list_index1)
    #     print("list_index2", list_index2)
    #     print("list_index3", list_index3)
    #     print("list_index4", list_index4)

    gt_side_index = np.array(list_index4).astype(np.int32)  # 得到了边界gt框的索引

    # 要得到与这些gt框有最大的overlap的anchors的索引，这些anchor是我们关心的
    gt_argmax_overlaps = overlaps.argmax(axis=0)
    anchor_side_index = gt_argmax_overlaps[gt_side_index]  # 得到143个与gt具有最大的overlaps的anchor的索引
    # 还要去掉与边界框overlap为0的anchor，因为这些anhcor不是真的我们关心的anchor，如果不去除，还会造成o_loss异常大
    anchor_side_list = list()
    for i in range(anchor_side_index.shape[0]):
        anchor_index = anchor_side_index[i]
        gt_index = gt_side_index[i]
        overlap = overlaps[anchor_index, gt_index]
        if overlap>0:
            anchor_side_list.append(anchor_index)
    anchor_side_index = np.array(anchor_side_list,dtype=np.int32)



    anchor_side_index1 = np.array(sorted(list(set(list(anchor_side_index))))).astype(np.int32)
    k_index = anchor_side_index1   # (s,) s个边界索引，但是并不是包括之前去除的超过边界框的索引值，所以需要之后的操作

    k_index1 = np.zeros((len(inds_inside)), dtype=np.int32)
    k_index1[k_index] = 1

    if DEBUG:
        print('jIndex1:',j_index1.shape)
        print('k_index1:',k_index1.shape)


    # map up to original set of anchors
    # 一开始是将超出图像范围的anchor直接丢掉的，现在在加回来
    labels = _unmap(labels, total_anchors, inds_inside, fill=-1)  # 这些anchor的label是-1，也即dontcare
    v_target = _unmap(v_target, total_anchors, inds_inside, fill=0)  # 这些anchor的真值是0，也即没有值
    o_target = _unmap(o_target, total_anchors, inds_inside, fill = 0)
    j_index2 = _unmap(j_index1, total_anchors, inds_inside, fill = 0).astype(np.int32)
    k_index2 = _unmap(k_index1, total_anchors, inds_inside, fill = 0).astype(np.int32)

    # real_j_index = np.where(j_index2==1)[0]
    # real_k_index = np.where(k_index2==1)[0]


    if DEBUG:
        print('loss_1 index:',np.where(labels!=-1)[0].shape[0])
        print('j_index:', j_index.shape)
        print('k_index:', k_index.shape)
        print('j_index2:', j_index2.shape)
        print('k_index2:', k_index2.shape)

        print('label shape', labels.shape)

        print('v_target shape', v_target.shape)
        print('o_target shape', o_target.shape)
    return labels, v_target, o_target, j_index2, k_index2



def _unmap(data, count, inds, fill=0):
    """ Unmap a subset of item (data) back to the original set of items (of
    size count) """
    if len(data.shape) == 1:
        ret = np.empty((count, ), dtype=np.float32)
        ret.fill(fill)
        ret[inds] = data
    else:
        ret = np.empty((count, ) + data.shape[1:], dtype=np.float32)
        ret.fill(fill)
        ret[inds, :] = data
    return ret

def _compute_targets(ex_rois, gt_rois):
    """Compute bounding-box regression targets for an image."""

    assert ex_rois.shape[0] == gt_rois.shape[0]
    assert ex_rois.shape[1] == 4
    assert gt_rois.shape[1] == 5

    target_v, target_o = v_compute(ex_rois, gt_rois[:, :4])
    return target_v.astype(np.float32, copy=False), target_o.astype(np.float32, copy=False)

def v_compute(ex_rois, gt_rois):
    """
    计算竖直方向坐标的回归目标
    :param ex_rois:
    :param gt_rois:
    :return: target_d (n*2)  v_c, v_h
    """
    ex_widths = ex_rois[:, 2] - ex_rois[:, 0] + 1.0
    ex_heights = ex_rois[:, 3] - ex_rois[:, 1] + 1.0
    ex_ctr_x = ex_rois[:, 0] + 0.5 * ex_widths
    ex_ctr_y = ex_rois[:, 1] + 0.5 * ex_heights

    assert np.min(ex_widths) > 0.1 and np.min(ex_heights) > 0.1, \
        'Invalid boxes found: {} {}'. \
            format(ex_rois[np.argmin(ex_widths), :], ex_rois[np.argmin(ex_heights), :])

    gt_widths = gt_rois[:, 2] - gt_rois[:, 0] + 1.0
    # 得到的gt_width怎么会有17
    gt_heights = gt_rois[:, 3] - gt_rois[:, 1] + 1.0
    gt_ctr_x = gt_rois[:, 0] + 0.5 * gt_widths
    gt_ctr_y = gt_rois[:, 1] + 0.5 * gt_heights

    target_dvc = (gt_ctr_y - ex_ctr_y) / ex_heights
    target_dvh = np.log(gt_heights / ex_heights)

    # 由于上一行报错，
    if not (target_dvh==target_dvh).all(): # 判断是否是nan
        print('gt_heights:', gt_heights, '\nex_heights:', ex_heights)
    target_v= np.vstack(
        (target_dvc, target_dvh)).transpose()

    target_do = (gt_ctr_x - ex_ctr_x) / ex_widths
    target_o = target_do

    return target_v, target_o


if __name__ == '__main__':
    from lib.fast_rcnn.train import get_data_layer, get_training_roidb
    from lib.datasets.factory import get_imdb

    imdb = get_imdb('voc_2007_trainval')
    roidb = get_training_roidb(imdb)
    data_layer = get_data_layer(roidb, 2)


    while True:
        db_inds, blobs = data_layer.forward()

        if blobs['im_name']!='auto_50_5768038962_20180628224921_20180629100000_161.jpg':
            continue

        data = blobs['data']
        im_info = blobs['im_info']
        gt_boxes = blobs['gt_boxes']
        gt_ishard = blobs['gt_ishard']
        dontcare_areas = blobs['dontcare_areas']
        rpn_cls_score = np.zeros((1, 30, 62, 20))
        a, b, c, d, e = anchor_target_layer(rpn_cls_score, gt_boxes, gt_ishard, dontcare_areas, im_info)
