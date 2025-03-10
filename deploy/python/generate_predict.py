# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import division

import cv2
import numpy as np
from PIL import Image, ImageFile

from ppdet.utils.logger import setup_logger as get_logger

ImageFile.LOAD_TRUNCATED_IMAGES = True

logger = get_logger(__name__)


def generate_box_mask(im, results, labels, threshold=0.5):
    """
    Args:
        im (str/np.ndarray): path of image/np.ndarray read by cv2
        results (dict): include 'boxes': np.ndarray: shape:[N,6], N: number of box,
                        matix element:[class, score, x_min, y_min, x_max, y_max]
                        MaskRCNN's results include 'masks': np.ndarray:
                        shape:[N, im_h, im_w]
        labels (list): labels:['class1', ..., 'classn']
        threshold (float): Threshold of score.
    Returns:
        im (PIL.Image.Image): visualized image
    """
    if 'masks' in results and 'boxes' in results and len(results['boxes']) > 0:
        im = generate_mask(
            im, results['boxes'], results['masks'], labels, threshold=threshold)
    if 'boxes' in results and len(results['boxes']) > 0:
        distinguish = generate_box(results['boxes'], labels, threshold=threshold)
    if 'segm' in results:
        im = generate_segm(
            im,
            results['segm'],
            results['label'],
            results['score'],
            labels,
            threshold=threshold)
    return im, distinguish


def get_color_map_list(num_classes):
    """
    Args:
        num_classes (int): number of class
    Returns:
        color_map (list): RGB color list
    """
    color_map = num_classes * [0, 0, 0]
    for i in range(0, num_classes):
        j = 0
        lab = i
        while lab:
            color_map[i * 3] |= (((lab >> 0) & 1) << (7 - j))
            color_map[i * 3 + 1] |= (((lab >> 1) & 1) << (7 - j))
            color_map[i * 3 + 2] |= (((lab >> 2) & 1) << (7 - j))
            j += 1
            lab >>= 3
    color_map = [color_map[i:i + 3] for i in range(0, len(color_map), 3)]
    return color_map


def generate_mask(im, np_boxes, np_masks, labels, threshold=0.5):
    """
    Args:
        im (PIL.Image.Image): PIL image
        np_boxes (np.ndarray): shape:[N,6], N: number of box,
            matix element:[class, score, x_min, y_min, x_max, y_max]
        np_masks (np.ndarray): shape:[N, im_h, im_w]
        labels (list): labels:['class1', ..., 'classn']
        threshold (float): threshold of mask
    Returns:
        im (PIL.Image.Image): visualized image
    """
    color_list = get_color_map_list(len(labels))
    w_ratio = 0.4
    alpha = 0.7
    im = np.array(im).astype('float32')
    clsid2color = {}
    expect_boxes = (np_boxes[:, 1] > threshold) & (np_boxes[:, 0] > -1)
    np_boxes = np_boxes[expect_boxes, :]
    np_masks = np_masks[expect_boxes, :, :]
    im_h, im_w = im.shape[:2]
    np_masks = np_masks[:, :im_h, :im_w]
    for i in range(len(np_masks)):
        clsid, score = int(np_boxes[i][0]), np_boxes[i][1]
        mask = np_masks[i]
        if clsid not in clsid2color:
            clsid2color[clsid] = color_list[clsid]
        color_mask = clsid2color[clsid]
        for c in range(3):
            color_mask[c] = color_mask[c] * (1 - w_ratio) + w_ratio * 255
        idx = np.nonzero(mask)
        color_mask = np.array(color_mask)
        im[idx[0], idx[1], :] *= 1.0 - alpha
        im[idx[0], idx[1], :] += alpha * color_mask
    return Image.fromarray(im.astype('uint8'))


def generate_box(np_boxes, labels, threshold=0.5):
    """
    Args:
        np_boxes (np.ndarray): shape:[N,6], N: number of box,
                               matix element:[class, score, x_min, y_min, x_max, y_max]
        labels (list): labels:['class1', ..., 'classn']
        threshold (float): threshold of box
    Returns:
        im (PIL.Image.Image): visualized image
    """
    expect_boxes = (np_boxes[:, 1] > threshold) & (np_boxes[:, 0] > -1)
    np_boxes = np_boxes[expect_boxes, :]

    distinguish = []
    for dt in np_boxes:
        clsid, bbox, score = int(dt[0]), dt[2:], dt[1]

        if len(bbox) == 4:
            xmin, ymin, xmax, ymax = bbox
            logger.debug('class_id:{:d}, confidence:{:.4f}, left_top:[{:.2f},{:.2f}],'
                         'right_bottom:[{:.2f},{:.2f}]'.format(
                int(clsid), score, xmin, ymin, xmax, ymax))
        elif len(bbox) == 8:
            x1, y1, x2, y2, x3, y3, x4, y4 = bbox

        distinguish.append({
            'class_id': int(clsid),
            'label': labels[clsid],
            'confidence': score,
            'left': xmin,
            'top': ymin,
            'right': xmax,
            'bottom': ymax
        })
    return distinguish


def generate_segm(im,
                  np_segms,
                  np_label,
                  np_score,
                  labels,
                  threshold=0.5,
                  alpha=0.7):
    """
    Draw segmentation on image
    """
    mask_color_id = 0
    w_ratio = .4
    color_list = get_color_map_list(len(labels))
    im = np.array(im).astype('float32')
    clsid2color = {}
    np_segms = np_segms.astype(np.uint8)
    for i in range(np_segms.shape[0]):
        mask, score, clsid = np_segms[i], np_score[i], np_label[i]
        if score < threshold:
            continue

        if clsid not in clsid2color:
            clsid2color[clsid] = color_list[clsid]
        color_mask = clsid2color[clsid]
        for c in range(3):
            color_mask[c] = color_mask[c] * (1 - w_ratio) + w_ratio * 255
        idx = np.nonzero(mask)
        color_mask = np.array(color_mask)
        idx0 = np.minimum(idx[0], im.shape[0] - 1)
        idx1 = np.minimum(idx[1], im.shape[1] - 1)
        im[idx0, idx1, :] *= 1.0 - alpha
        im[idx0, idx1, :] += alpha * color_mask
        sum_x = np.sum(mask, axis=0)
        x = np.where(sum_x > 0.5)[0]
        sum_y = np.sum(mask, axis=1)
        y = np.where(sum_y > 0.5)[0]
        x0, x1, y0, y1 = x[0], x[-1], y[0], y[-1]
        cv2.rectangle(im, (x0, y0), (x1, y1),
                      tuple(color_mask.astype('int32').tolist()), 1)
        bbox_text = '%s %.2f' % (labels[clsid], score)
        t_size = cv2.getTextSize(bbox_text, 0, 0.3, thickness=1)[0]
        cv2.rectangle(im, (x0, y0), (x0 + t_size[0], y0 - t_size[1] - 3),
                      tuple(color_mask.astype('int32').tolist()), -1)
        cv2.putText(
            im,
            bbox_text, (x0, y0 - 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.3, (0, 0, 0),
            1,
            lineType=cv2.LINE_AA)
    return Image.fromarray(im.astype('uint8'))


def get_color(idx):
    idx = idx * 3
    color = ((37 * idx) % 255, (17 * idx) % 255, (29 * idx) % 255)
    return color
