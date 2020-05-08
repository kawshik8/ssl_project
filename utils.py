import numpy as np
from matplotlib.path import Path
import cv2
import torch

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision

from shapely.geometry import Polygon

TRANSFORM = torchvision.transforms.ToTensor()

def to_np(x):
    return x.detach().cpu().data.numpy()


def bounding_boxes_to_segmentation(full_width, full_height, scale, bounding_boxes, categories):
    out = torch.zeros((full_height, full_width), dtype=torch.long)
    
    ind = np.indices((full_height, full_width))
    ind[0] = ind[0] - full_height / 2
    ind[1] = ind[1] - full_width / 2
    ind = np.moveaxis(ind, 0, 2)
    for i, b in enumerate(bounding_boxes.detach().data.numpy()):
        p = Path([b[:,0]*scale,b[:,1]*scale,b[:,3]*scale,b[:,2]*scale])
        g = p.contains_points(ind.reshape(full_width*full_height,2))
        g = np.flip(g.reshape(full_height, full_width), axis=1).T
        g = g.copy()
        out[g] = 1

    return out

def get_bounding_boxes_from_seg(segment_tensor, scale, full_height, full_width):
    contours, _ = cv2.findContours(to_np(segment_tensor).astype(np.int32), cv2.RETR_FLOODFILL, cv2.CHAIN_APPROX_SIMPLE)

    contours_poly = [None]*len(contours)
    boundRect = [None]*len(contours)
    for i, c in enumerate(contours):
        contours_poly[i] = cv2.approxPolyDP(c, 3, True)
        boundRect[i] = cv2.boundingRect(contours_poly[i])
        
    def convert(value, dim):
        if dim == "x":
            out = ( value - full_width / 2 ) / scale
        elif dim == "y":
             out = -( value - full_height / 2 ) / scale
        return out
    for i in range(len(boundRect)):
        boundRect[i] = [[convert(boundRect[i][0] + boundRect[i][2], "x"), convert(boundRect[i][1], "y")], 
                        [convert(boundRect[i][0] + boundRect[i][2], "x"), convert(boundRect[i][1] +boundRect[i][3], "y")],
                        [convert(boundRect[i][0], "x"), convert(boundRect[i][1], "y")],
                        [convert(boundRect[i][0], "x"), convert(boundRect[i][1] + boundRect[i][3], "y")]
                        ]
    filteredBoundRect = []
    for i in range(len(boundRect)):
        if ( ( boundRect[i][0][0] - boundRect[i][2][0] ) > 1) and (( boundRect[i][0][1] - boundRect[i][1][1] )>1):
            filteredBoundRect.append(boundRect[i])
    if len(filteredBoundRect) > 0:
        return torch.FloatTensor(filteredBoundRect).permute(0,2,1)
    else:
        return torch.zeros((1,2,4))

def compute_ats_bounding_boxes_NEW(pred_boxes_p24, targ_boxes_t24):
    n_p, n_t  = pred_boxes_p24.size(0), targ_boxes_t24.size(0)

    def get_min_max_x_y_n(boxes_n24):
        min_x_n, max_x_n = boxes_n24[:, 0].min(dim=1)[0], boxes_n24[:, 0].max(dim=1)[0]
        min_y_n, max_y_n = boxes_n24[:, 1].min(dim=1)[0], boxes_n24[:, 1].max(dim=1)[0]
        return min_x_n, max_x_n, min_y_n, max_y_n

    min_x_p, max_x_p, min_y_p, max_y_p = get_min_max_x_y_n(pred_boxes_p24)
    min_x_t, max_x_t, min_y_t, max_y_t = get_min_max_x_y_n(targ_boxes_t24)

    condition_matrix_pt = (
             (max_x_p[:, None] > min_x_t[None, :])
        &  (min_x_p[:, None] < max_x_t[None, :])
        &  (max_y_p[:, None] > min_y_t[None, :])
        &  (min_y_p[:, None] < max_y_t[None, :])
    )

    iou_matrix_pt = torch.zeros(n_p, n_t)
    for p_idx in range(n_p):
        for t_idx in range(n_t):
            if condition_matrix_pt[p_idx, t_idx]:
                iou_matrix_pt[p_idx][t_idx] = compute_iou(pred_boxes_p24[p_idx], targ_boxes_t24[t_idx])

    iou_max_t = iou_matrix_pt.max(dim=0)[0]


    thresholds_k = torch.Tensor([0.5, 0.6, 0.7, 0.8, 0.9])
    weight_k     = 1. / thresholds_k
    tp_kt = (iou_max_t[None, :] > thresholds_k[:, None]).float()
    tp_k  = tp_kt.sum(dim=1)
    threat_score_k = tp_k * 1.0 / (n_p + n_t - tp_k)
    
    return weight_k.dot(threat_score_k) / weight_k.sum()

def compute_ats_bounding_boxes(boxes1, boxes2):
    num_boxes1 = boxes1.size(0)
    num_boxes2 = boxes2.size(0)

    boxes1_max_x = boxes1[:, 0].max(dim=1)[0]
    boxes1_min_x = boxes1[:, 0].min(dim=1)[0]
    boxes1_max_y = boxes1[:, 1].max(dim=1)[0]
    boxes1_min_y = boxes1[:, 1].min(dim=1)[0]

    boxes2_max_x = boxes2[:, 0].max(dim=1)[0]
    boxes2_min_x = boxes2[:, 0].min(dim=1)[0]
    boxes2_max_y = boxes2[:, 1].max(dim=1)[0]
    boxes2_min_y = boxes2[:, 1].min(dim=1)[0]

    condition1_matrix = (boxes1_max_x.unsqueeze(1) > boxes2_min_x.unsqueeze(0))
    condition2_matrix = (boxes1_min_x.unsqueeze(1) < boxes2_max_x.unsqueeze(0))
    condition3_matrix = (boxes1_max_y.unsqueeze(1) > boxes2_min_y.unsqueeze(0))
    condition4_matrix = (boxes1_min_y.unsqueeze(1) < boxes2_max_y.unsqueeze(0))
    condition_matrix = condition1_matrix * condition2_matrix * condition3_matrix * condition4_matrix

    iou_matrix = torch.zeros(num_boxes1, num_boxes2)
    for i in range(num_boxes1):
        for j in range(num_boxes2):
            if condition_matrix[i][j]:
                iou_matrix[i][j] = compute_iou(boxes1[i], boxes2[j])

    iou_max = iou_matrix.max(dim=0)[0]

    iou_thresholds = [0.5, 0.6, 0.7, 0.8, 0.9]
    total_threat_score = 0
    total_weight = 0
    for threshold in iou_thresholds:
        tp = (iou_max > threshold).sum()
        threat_score = tp * 1.0 / (num_boxes1 + num_boxes2 - tp)
        total_threat_score += 1.0 / threshold * threat_score
        total_weight += 1.0 / threshold

    average_threat_score = total_threat_score / total_weight
    
    return average_threat_score

def compute_ts_road_map(road_map1, road_map2):
    tp = (road_map1 * road_map2).sum()

    return tp * 1.0 / (road_map1.sum() + road_map2.sum() - tp)

def compute_iou(box1, box2):
    a = Polygon(torch.t(box1)).convex_hull
    b = Polygon(torch.t(box2)).convex_hull
    
    return a.intersection(b).area / a.union(b).area
