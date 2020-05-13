import torch
import numpy as np
import random
import cv2

###
# Util funcs
###
    
def to_np(x):
    return x.detach().cpu().data.numpy()

def my_transpose(arr, axis_1, axis_2):
    """works both for pytorch and numpy"""
    try:
        return arr.transpose(axis_1, axis_2)
    except:
        return arr.swapaxes(axis_1, axis_2)
    
def my_copy(arr):
    """works both for pytorch and numpy"""
    try:
        return arr.clone()
    except:
        return arr.copy()

def convert_to_bb_space(points_a2, axis=-1):
    """
    a: 
        any shape
    axis: 
        axis of dim=2 -- (x, y) 
    """
    assert points_a2.shape[axis] == 2, f"axis={axis} should have size 2: (x, y)"
    points_a2 = my_transpose(my_copy(points_a2), axis, -1)
    
    points_a2[..., 1] = 800 - points_a2[..., 1]
    ans_a2 = (points_a2 - 400) / 10
    
    return my_transpose(ans_a2, axis, -1)

def convert_from_bb_space(points_a2, axis=-1):
    """
    a: 
        any shape
    axis: 
        axis of dim=2 -- (x, y) 
    """
    assert points_a2.shape[axis] == 2, f"axis={axis} should have size 2: (x, y)"
    points_a2 = my_transpose(my_copy(points_a2), axis, -1)
        
    points_a2 = points_a2 * 10 + 400
    points_a2[..., 1] = 800 - points_a2[..., 1]
    
    return my_transpose(points_a2, axis, -1)
    
###
# Process segmentation maps as bounding boxes
###

class ProcessSegmentationMaps:
    def __init__(self, 
                 len_x=50, 
                 min_x_len=0.6 * 45, min_y_len=0.6 * 20, 
                 min_y_coord=100, max_y_coord=600):
        """
        Assumes cars are parallel to main road.

        len_x : length of car
        """
        self.len_x = len_x
        self.min_x_len = min_x_len
        self.min_y_len = min_y_len
        self.min_y_coord = min_y_coord
        self.max_y_coord = max_y_coord
        self.convert_to_bb_space = convert_to_bb_space
        self.convert_from_bb_space = convert_from_bb_space
        
    def transform(self, segm_map_NN, input_type="segm", threshold=0.5):
        assert input_type in ("segm", "bb")
        if input_type == "segm":
            bbs_k24 = self.get_bounding_boxes_from_segm(segm_map_NN, threshold)
        elif input_type == "bb":
            bbs_k24 = segm_map_NN
        filtered_bbs_k24 = [self._filter_one_car(bb_24) for bb_24 in bbs_k24]
        return torch.cat(filtered_bbs_k24) if len(filtered_bbs_k24) > 0 else torch.ones((0, 2, 4))
    
    def get_bounding_boxes_from_segm(self, segm_map_NN, threshold):
        bin_map_NN = to_np(segm_map_NN > threshold).astype("uint8")
        contours_k, _ = cv2.findContours(bin_map_NN, 
                                         cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        contours_poly_k = [None] * len(contours_k)
        bb_k24 = torch.zeros((len(contours_k), 2, 4))

        for idx, contour in enumerate(contours_k):
            contour_poly = cv2.approxPolyDP(contour, 3, True)
            x, y, w, h = cv2.boundingRect(contour_poly)
            bb_k24[idx] = torch.Tensor([
                [x + w, x + w, x,   x    ],
                [y,     y + h, y,   y + h],
            ])
        return bb_k24

    def _filter_one_car(self, bb_24):
        """return bb_k24 where k >= 0"""
        x_min, x_max = bb_24[0].min(dim=-1)[0], bb_24[0].max(dim=-1)[0]
        y_min, y_max = bb_24[1].min(dim=-1)[0], bb_24[1].max(dim=-1)[0]
        # Remove if car is short
        if (x_max - x_min < self.min_x_len) or (y_max - y_min < self.min_y_len):
            return torch.ones((0, 2, 4))
        # Remove if car is outside of the road region
        if y_max > self.max_y_coord or y_min < self.min_y_coord:
            return torch.ones((0, 2, 4))
        # Split if long
        num_short_cars_in_bb = (x_max - x_min) / self.len_x
        num_breaks = max(2, round(num_short_cars_in_bb.item()) + 1)
        break_points = torch.linspace(x_min, x_max, num_breaks)

        splitted_bbs_list = []
        for new_min, new_max in zip(break_points[:-1], break_points[1:]):
            cur_bb_24 = bb_24.clone()
            cur_bb_24[0] = torch.Tensor([new_max, new_max, new_min, new_min])
            splitted_bbs_list.append(cur_bb_24)

        return torch.stack(splitted_bbs_list)
    
###
# Baseline for cars
###
def get_baseline_raw_bbs(in_bb_space=False):
    """ returns grid_bbs_k24"""
    grid_bbs = []

    len_x, len_y = 50., 25.
    offset_x, from_y, to_y = 20., 420., 440 
    for x_start in np.arange(offset_x, 800 - offset_x, len_x):
        for y_start in np.arange(from_y, to_y, len_y):
            bb_24 = torch.Tensor([
                [x_start + len_x, x_start + len_x, x_start, x_start],
                [y_start + len_y, y_start, y_start + len_y, y_start],
            ])
            grid_bbs.append(bb_24)

    grid_bbs_k24 = torch.stack(grid_bbs)
    if in_bb_space:
        grid_bbs_k24 = convert_to_bb_space(grid_bbs_k24, axis=-2)
    return grid_bbs_k24
    
###
# Generate data for experiments
###

def _generate_random_bbs_of_fixed_car_size(k, size_xy=(40, 20)):
    """
    returns: random_bbs_k24
    """
    random_k2 = torch.Tensor(np.random.randint(0, 800, size=(k, 2)).astype(float))
    random_bbs_k24 = torch.zeros(k, 2, 4)
    random_bbs_k24 += random_k2[..., None]

    # shift_24 = torch.Tensor(np.random.randint(0, 40, size=(2, 4)).astype(float))
    shift_24 = torch.Tensor([
        [0, 0, -size_xy[0], -size_xy[0]],
        [0, -size_xy[1], 0, -size_xy[1]]
    ]).float()
    random_bbs_k24 += shift_24[None]
    return random_bbs_k24

def generate_random_bbs(k):
    """
    returns: random_bbs_k24
    """
    random_bbs_k24 = []
    for _ in range(k):
        x, y = random.randint(2, 200), random.randint(2, 20)
        random_bbs_k24.append(_generate_random_bbs_of_fixed_car_size(1, (x, y)))
    return torch.cat(random_bbs_k24)