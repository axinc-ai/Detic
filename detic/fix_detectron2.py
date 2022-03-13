from typing import List, Tuple

import torch
from torchvision.ops import batched_nms

from detectron2.layers import cat, shapes_to_tensor
from detectron2.structures import Instances, Boxes
from detectron2.modeling import poolers
from detectron2.modeling.roi_heads import mask_head
from detectron2.modeling.roi_heads import fast_rcnn


def instance_len(self) -> int:
    for v in self._fields.values():
        # use __len__ because len() has to be int and is not friendly to tracing
        return v.shape[0] if hasattr(v, "shape") else v.__len__()
    raise NotImplementedError("Empty Instances does not support __len__!")


def convert_boxes_to_pooler_format(box_lists: List[Boxes]):
    boxes = torch.cat([x.tensor for x in box_lists], dim=0)
    # __len__ returns Tensor in tracing.
    sizes = shapes_to_tensor([x.__len__() for x in box_lists], device=boxes.device)
    return cat([torch.zeros(boxes.shape[0], dtype=boxes.dtype, device=boxes.device).view(-1,1), boxes], dim=1)


def mask_rcnn_inference(pred_mask_logits: torch.Tensor, pred_instances: List[Instances]):
    cls_agnostic_mask = pred_mask_logits.size(1) == 1

    if cls_agnostic_mask:   ### ignore
        mask_probs_pred = pred_mask_logits.sigmoid()
    else:
        # Select masks corresponding to the predicted classes
        num_masks = pred_mask_logits.shape[0]
        class_pred = cat([i.pred_classes for i in pred_instances])
        indices = torch.arange(num_masks, device=class_pred.device)
        mask_probs_pred = pred_mask_logits[indices, class_pred][:, None].sigmoid()

    mask_probs_pred = [mask_probs_pred]

    for prob, instances in zip(mask_probs_pred, pred_instances):
        instances.pred_masks = prob  # (1, Hmask, Wmask)


def fast_rcnn_inference_single_image(
    boxes,
    scores,
    image_shape: Tuple[int, int],
    score_thresh: float,
    nms_thresh: float,
    topk_per_image: int,
):
    scores = scores[:, :-1]
    num_bbox_reg_classes = boxes.shape[1] // 4  ### ignore
    # Convert to Boxes to use the `clip` function ...
    boxes = Boxes(boxes.reshape(-1, 4))
    boxes.clip(image_shape)
    boxes = boxes.tensor.view(-1, num_bbox_reg_classes, 4)  # R x C x 4

    # 1. Filter results based on detection scores. It can make NMS more efficient
    #    by filtering out low-confidence detections.
    filter_mask = scores > score_thresh  # R x K
    # R' x 2. First column contains indices of the R predictions;
    # Second column contains indices of classes.
    filter_inds = filter_mask.nonzero()
    if num_bbox_reg_classes == 1:   ### ignore
        boxes = boxes[filter_inds[:, 0], 0]
    else:
        boxes = boxes[filter_mask]
    scores = scores[filter_mask]

    # 2. Apply NMS for each class independently.
    keep = batched_nms(boxes, scores, filter_inds[:, 1], nms_thresh)
    if topk_per_image >= 0:
        keep = keep[:topk_per_image]
    boxes, scores, filter_inds = boxes[keep], scores[keep], filter_inds[keep]

    result = Instances(image_shape)
    result.pred_boxes = Boxes(boxes)
    result.scores = scores
    result.pred_classes = filter_inds[:, 1]
    return result, filter_inds[:, 0]


def fix_detectron2():
    setattr(Instances, "__len__", instance_len)
    poolers.convert_boxes_to_pooler_format = convert_boxes_to_pooler_format
    mask_head.mask_rcnn_inference = mask_rcnn_inference
    fast_rcnn.fast_rcnn_inference_single_image = fast_rcnn_inference_single_image
