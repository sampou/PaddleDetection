# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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

import glob
import json
import math
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import paddle
import yaml

from paddle.inference import Config
from paddle.inference import create_predictor

# add deploy path of PadleDetection to sys.path
parent_path = os.path.abspath(os.path.join(__file__, *(['..'])))
sys.path.insert(0, parent_path)

from benchmark_utils import PaddleInferBenchmark
from picodet_postprocess import PicoDetPostProcess
from preprocess import preprocess, Resize, NormalizeImage, Permute, PadStride, LetterBoxResize, WarpAffine, Pad, \
    decode_image
from keypoint_preprocess import EvalAffine, TopDownEvalAffine, expand_crop
from visualize import visualize_box_mask
from generate_predict import generate_box_mask
from utils import argsparser, Timer, get_current_memory_mb
from ppdet.utils.logger import setup_logger as get_logger
import argparse
import logging

__all__ = ['Predict']

logger = get_logger(__name__)

# Global dictionary
SUPPORT_MODELS = {
    'YOLO',
    'RCNN',
    'SSD',
    'Face',
    'FCOS',
    'SOLOv2',
    'TTFNet',
    'S2ANet',
    'JDE',
    'FairMOT',
    'DeepSORT',
    'GFL',
    'PicoDet',
    'CenterNet',
    'TOOD',
    'RetinaNet',
    'StrongBaseline',
    'STGCN',
    'YOLOX',
}


def bench_log(detector, img_list, model_info, batch_size=1, name=None):
    mems = {
        'cpu_rss_mb': detector.cpu_mem / len(img_list),
        'gpu_rss_mb': detector.gpu_mem / len(img_list),
        'gpu_util': detector.gpu_util * 100 / len(img_list)
    }
    perf_info = detector.det_times.report(average=True)
    data_info = {
        'batch_size': batch_size,
        'shape': "dynamic_shape",
        'data_num': perf_info['img_num']
    }
    log = PaddleInferBenchmark(detector.config, model_info, data_info,
                               perf_info, mems)
    log(name)


class Detector(object):
    """
    Args:
        pred_config (object): config of model, defined by `Config(model_dir)`
        model_dir (str): root path of model.pdiparams, model.pdmodel and infer_cfg.yml
        device (str): Choose the device you want to run, it can be: CPU/GPU/XPU, default is CPU
        run_mode (str): mode of running(paddle/trt_fp32/trt_fp16)
        batch_size (int): size of pre batch in inference
        trt_min_shape (int): min shape for dynamic shape in trt
        trt_max_shape (int): max shape for dynamic shape in trt
        trt_opt_shape (int): opt shape for dynamic shape in trt
        trt_calib_mode (bool): If the model is produced by TRT offline quantitative
            calibration, trt_calib_mode need to set True
        cpu_threads (int): cpu threads
        enable_mkldnn (bool): whether to open MKLDNN
        enable_mkldnn_bfloat16 (bool): whether to turn on mkldnn bfloat16
        output_dir (str): The path of output
        threshold (float): The threshold of score for visualization
        delete_shuffle_pass (bool): whether to remove shuffle_channel_detect_pass in TensorRT. 
                                    Used by action model.
    """

    def __init__(self,
                 model_dir,
                 device='CPU',
                 run_mode='paddle',
                 batch_size=1,
                 trt_min_shape=1,
                 trt_max_shape=1280,
                 trt_opt_shape=640,
                 trt_calib_mode=False,
                 cpu_threads=1,
                 enable_mkldnn=False,
                 enable_mkldnn_bfloat16=False,
                 output_dir='output',
                 threshold=0.5,
                 delete_shuffle_pass=False):
        self.pred_config = self.set_config(model_dir)
        self.predictor, self.config = load_predictor(
            model_dir,
            run_mode=run_mode,
            batch_size=batch_size,
            min_subgraph_size=self.pred_config.min_subgraph_size,
            device=device,
            use_dynamic_shape=self.pred_config.use_dynamic_shape,
            trt_min_shape=trt_min_shape,
            trt_max_shape=trt_max_shape,
            trt_opt_shape=trt_opt_shape,
            trt_calib_mode=trt_calib_mode,
            cpu_threads=cpu_threads,
            enable_mkldnn=enable_mkldnn,
            enable_mkldnn_bfloat16=enable_mkldnn_bfloat16,
            delete_shuffle_pass=delete_shuffle_pass)
        self.det_times = Timer()
        self.cpu_mem, self.gpu_mem, self.gpu_util = 0, 0, 0
        self.batch_size = batch_size
        self.output_dir = output_dir
        self.threshold = threshold

    def set_config(self, model_dir):
        return PredictConfig(model_dir)

    def preprocess(self, image_list):
        preprocess_ops = []
        for op_info in self.pred_config.preprocess_infos:
            new_op_info = op_info.copy()
            op_type = new_op_info.pop('type')
            preprocess_ops.append(eval(op_type)(**new_op_info))

        input_im_lst = []
        input_im_info_lst = []
        for im_path in image_list:
            im, im_info = preprocess(im_path, preprocess_ops)
            input_im_lst.append(im)
            input_im_info_lst.append(im_info)
        inputs = create_inputs(input_im_lst, input_im_info_lst)
        input_names = self.predictor.get_input_names()
        for i in range(len(input_names)):
            input_tensor = self.predictor.get_input_handle(input_names[i])
            input_tensor.copy_from_cpu(inputs[input_names[i]])

        return inputs

    def postprocess(self, inputs, result):
        # postprocess output of predictor
        np_boxes_num = result['boxes_num']
        if np_boxes_num[0] <= 0:
            logger.warning('[WARNNING] No object detected.')
            result = {'boxes': np.zeros([0, 6]), 'boxes_num': [0]}
        result = {k: v for k, v in result.items() if v is not None}
        return result

    def filter_box(self, result, threshold):
        np_boxes_num = result['boxes_num']
        boxes = result['boxes']
        start_idx = 0
        filter_boxes = []
        filter_num = []
        for i in range(len(np_boxes_num)):
            boxes_num = np_boxes_num[i]
            boxes_i = boxes[start_idx:start_idx + boxes_num, :]
            idx = boxes_i[:, 1] > threshold
            filter_boxes_i = boxes_i[idx, :]
            filter_boxes.append(filter_boxes_i)
            filter_num.append(filter_boxes_i.shape[0])
            start_idx += boxes_num
        boxes = np.concatenate(filter_boxes)
        filter_num = np.array(filter_num)
        filter_res = {'boxes': boxes, 'boxes_num': filter_num}
        return filter_res

    def predict(self, repeats=1):
        '''
        Args:
            repeats (int): repeats number for prediction
        Returns:
            result (dict): include 'boxes': np.ndarray: shape:[N,6], N: number of box,
                            matix element:[class, score, x_min, y_min, x_max, y_max]
                            MaskRCNN's result include 'masks': np.ndarray:
                            shape: [N, im_h, im_w]
        '''
        # model prediction
        np_boxes, np_masks = None, None
        for i in range(repeats):
            self.predictor.run()
            output_names = self.predictor.get_output_names()
            boxes_tensor = self.predictor.get_output_handle(output_names[0])
            np_boxes = boxes_tensor.copy_to_cpu()
            boxes_num = self.predictor.get_output_handle(output_names[1])
            np_boxes_num = boxes_num.copy_to_cpu()
            if self.pred_config.mask:
                masks_tensor = self.predictor.get_output_handle(output_names[2])
                np_masks = masks_tensor.copy_to_cpu()
        result = dict(boxes=np_boxes, masks=np_masks, boxes_num=np_boxes_num)
        return result

    def merge_batch_result(self, batch_result):
        if len(batch_result) == 1:
            return batch_result[0]
        res_key = batch_result[0].keys()
        results = {k: [] for k in res_key}
        for res in batch_result:
            for k, v in res.items():
                results[k].append(v)
        for k, v in results.items():
            if k != 'masks':
                results[k] = np.concatenate(v)
        return results

    def get_timer(self):
        return self.det_times

    def predict_image(self,
                      image_list,
                      run_benchmark=False,
                      repeats=1,
                      visual=True,
                      save_file=None):
        batch_loop_cnt = math.ceil(float(len(image_list)) / self.batch_size)
        results = []
        distinguishs = []
        for i in range(batch_loop_cnt):
            start_index = i * self.batch_size
            end_index = min((i + 1) * self.batch_size, len(image_list))
            batch_image_list = image_list[start_index:end_index]
            if run_benchmark:
                # preprocess
                inputs = self.preprocess(batch_image_list)  # warmup
                self.det_times.preprocess_time_s.start()
                inputs = self.preprocess(batch_image_list)
                self.det_times.preprocess_time_s.end()

                # model prediction
                result = self.predict(repeats=50)  # warmup
                self.det_times.inference_time_s.start()
                result = self.predict(repeats=repeats)
                self.det_times.inference_time_s.end(repeats=repeats)

                # postprocess
                result_warmup = self.postprocess(inputs, result)  # warmup
                self.det_times.postprocess_time_s.start()
                result = self.postprocess(inputs, result)
                self.det_times.postprocess_time_s.end()
                self.det_times.img_num += len(batch_image_list)

                cm, gm, gu = get_current_memory_mb()
                self.cpu_mem += cm
                self.gpu_mem += gm
                self.gpu_util += gu
            else:
                # preprocess
                self.det_times.preprocess_time_s.start()
                inputs = self.preprocess(batch_image_list)
                self.det_times.preprocess_time_s.end()

                # model prediction
                self.det_times.inference_time_s.start()
                result = self.predict()
                self.det_times.inference_time_s.end()

                # postprocess
                self.det_times.postprocess_time_s.start()
                result = self.postprocess(inputs, result)
                self.det_times.postprocess_time_s.end()
                self.det_times.img_num += len(batch_image_list)

                distinguishs.append(
                    generate_result(
                        batch_image_list,
                        result,
                        self.pred_config.labels,
                        self.threshold))

                if visual:
                    visualize(
                        batch_image_list,
                        result,
                        self.pred_config.labels,
                        output_dir=self.output_dir,
                        threshold=self.threshold)

            results.append(result)
            if visual:
                logger.info('Test iter {}'.format(i))

        if save_file is not None:
            Path(self.output_dir).mkdir(exist_ok=True)
            self.format_coco_results(image_list, results, save_file=save_file)

        results = self.merge_batch_result(results)
        return results, distinguishs

    def predict_video(self, video_file, camera_id):
        video_out_name = 'output.mp4'
        if camera_id != -1:
            capture = cv2.VideoCapture(camera_id)
        else:
            capture = cv2.VideoCapture(video_file)
            video_out_name = os.path.split(video_file)[-1]
        # Get Video info : resolution, fps, frame count
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = int(capture.get(cv2.CAP_PROP_FPS))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        logger.info("fps: %d, frame_count: %d" % (fps, frame_count))

        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
        out_path = os.path.join(self.output_dir, video_out_name)
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        writer = cv2.VideoWriter(out_path, fourcc, fps, (width, height))
        index = 1
        while (1):
            ret, frame = capture.read()
            if not ret:
                break
            logger.debug('detect frame: %d' % (index))
            index += 1
            results = self.predict_image([frame], visual=False)

            im = visualize_box_mask(
                frame,
                results,
                self.pred_config.labels,
                threshold=self.threshold)
            im = np.array(im)
            writer.write(im)
            if camera_id != -1:
                cv2.imshow('Mask Detection', im)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
        writer.release()

    @staticmethod
    def format_coco_results(image_list, results, save_file=None):
        coco_results = []
        image_id = 0

        for result in results:
            start_idx = 0
            for box_num in result['boxes_num']:
                idx_slice = slice(start_idx, start_idx + box_num)
                start_idx += box_num

                image_file = image_list[image_id]
                image_id += 1

                if 'boxes' in result:
                    boxes = result['boxes'][idx_slice, :]
                    per_result = [
                        {
                            'image_file': image_file,
                            'bbox':
                                [box[2], box[3], box[4] - box[2],
                                 box[5] - box[3]],  # xyxy -> xywh
                            'score': box[1],
                            'category_id': int(box[0]),
                        } for k, box in enumerate(boxes.tolist())
                    ]

                elif 'segm' in result:
                    import pycocotools.mask as mask_util

                    scores = result['score'][idx_slice].tolist()
                    category_ids = result['label'][idx_slice].tolist()
                    segms = result['segm'][idx_slice, :]
                    rles = [
                        mask_util.encode(
                            np.array(
                                mask[:, :, np.newaxis],
                                dtype=np.uint8,
                                order='F'))[0] for mask in segms
                    ]
                    for rle in rles:
                        rle['counts'] = rle['counts'].decode('utf-8')

                    per_result = [{
                        'image_file': image_file,
                        'segmentation': rle,
                        'score': scores[k],
                        'category_id': category_ids[k],
                    } for k, rle in enumerate(rles)]

                else:
                    raise RuntimeError('')

                # per_result = [item for item in per_result if item['score'] > threshold]
                coco_results.extend(per_result)

        if save_file:
            with open(os.path.join(save_file), 'w') as f:
                json.dump(coco_results, f)

        return coco_results


class DetectorSOLOv2(Detector):
    """
    Args:
        model_dir (str): root path of model.pdiparams, model.pdmodel and infer_cfg.yml
        device (str): Choose the device you want to run, it can be: CPU/GPU/XPU, default is CPU
        run_mode (str): mode of running(paddle/trt_fp32/trt_fp16)
        batch_size (int): size of pre batch in inference
        trt_min_shape (int): min shape for dynamic shape in trt
        trt_max_shape (int): max shape for dynamic shape in trt
        trt_opt_shape (int): opt shape for dynamic shape in trt
        trt_calib_mode (bool): If the model is produced by TRT offline quantitative
            calibration, trt_calib_mode need to set True
        cpu_threads (int): cpu threads
        enable_mkldnn (bool): whether to open MKLDNN 
        enable_mkldnn_bfloat16 (bool): Whether to turn on mkldnn bfloat16
        output_dir (str): The path of output
        threshold (float): The threshold of score for visualization
       
    """

    def __init__(
            self,
            model_dir,
            device='CPU',
            run_mode='paddle',
            batch_size=1,
            trt_min_shape=1,
            trt_max_shape=1280,
            trt_opt_shape=640,
            trt_calib_mode=False,
            cpu_threads=1,
            enable_mkldnn=False,
            enable_mkldnn_bfloat16=False,
            output_dir='./',
            threshold=0.5, ):
        super(DetectorSOLOv2, self).__init__(
            model_dir=model_dir,
            device=device,
            run_mode=run_mode,
            batch_size=batch_size,
            trt_min_shape=trt_min_shape,
            trt_max_shape=trt_max_shape,
            trt_opt_shape=trt_opt_shape,
            trt_calib_mode=trt_calib_mode,
            cpu_threads=cpu_threads,
            enable_mkldnn=enable_mkldnn,
            enable_mkldnn_bfloat16=enable_mkldnn_bfloat16,
            output_dir=output_dir,
            threshold=threshold, )

    def predict(self, repeats=1):
        '''
        Args:
            repeats (int): repeat number for prediction
        Returns:
            result (dict): 'segm': np.ndarray,shape:[N, im_h, im_w]
                            'cate_label': label of segm, shape:[N]
                            'cate_score': confidence score of segm, shape:[N]
        '''
        np_label, np_score, np_segms = None, None, None
        for i in range(repeats):
            self.predictor.run()
            output_names = self.predictor.get_output_names()
            np_boxes_num = self.predictor.get_output_handle(output_names[
                                                                0]).copy_to_cpu()
            np_label = self.predictor.get_output_handle(output_names[
                                                            1]).copy_to_cpu()
            np_score = self.predictor.get_output_handle(output_names[
                                                            2]).copy_to_cpu()
            np_segms = self.predictor.get_output_handle(output_names[
                                                            3]).copy_to_cpu()

        result = dict(
            segm=np_segms,
            label=np_label,
            score=np_score,
            boxes_num=np_boxes_num)
        return result


class DetectorPicoDet(Detector):
    """
    Args:
        model_dir (str): root path of model.pdiparams, model.pdmodel and infer_cfg.yml
        device (str): Choose the device you want to run, it can be: CPU/GPU/XPU, default is CPU
        run_mode (str): mode of running(paddle/trt_fp32/trt_fp16)
        batch_size (int): size of pre batch in inference
        trt_min_shape (int): min shape for dynamic shape in trt
        trt_max_shape (int): max shape for dynamic shape in trt
        trt_opt_shape (int): opt shape for dynamic shape in trt
        trt_calib_mode (bool): If the model is produced by TRT offline quantitative
            calibration, trt_calib_mode need to set True
        cpu_threads (int): cpu threads
        enable_mkldnn (bool): whether to turn on MKLDNN
        enable_mkldnn_bfloat16 (bool): whether to turn on MKLDNN_BFLOAT16
    """

    def __init__(
            self,
            model_dir,
            device='CPU',
            run_mode='paddle',
            batch_size=1,
            trt_min_shape=1,
            trt_max_shape=1280,
            trt_opt_shape=640,
            trt_calib_mode=False,
            cpu_threads=1,
            enable_mkldnn=False,
            enable_mkldnn_bfloat16=False,
            output_dir='./',
            threshold=0.5, ):
        super(DetectorPicoDet, self).__init__(
            model_dir=model_dir,
            device=device,
            run_mode=run_mode,
            batch_size=batch_size,
            trt_min_shape=trt_min_shape,
            trt_max_shape=trt_max_shape,
            trt_opt_shape=trt_opt_shape,
            trt_calib_mode=trt_calib_mode,
            cpu_threads=cpu_threads,
            enable_mkldnn=enable_mkldnn,
            enable_mkldnn_bfloat16=enable_mkldnn_bfloat16,
            output_dir=output_dir,
            threshold=threshold, )

    def postprocess(self, inputs, result):
        # postprocess output of predictor
        np_score_list = result['boxes']
        np_boxes_list = result['boxes_num']
        postprocessor = PicoDetPostProcess(
            inputs['image'].shape[2:],
            inputs['im_shape'],
            inputs['scale_factor'],
            strides=self.pred_config.fpn_stride,
            nms_threshold=self.pred_config.nms['nms_threshold'])
        np_boxes, np_boxes_num = postprocessor(np_score_list, np_boxes_list)
        result = dict(boxes=np_boxes, boxes_num=np_boxes_num)
        return result

    def predict(self, repeats=1):
        '''
        Args:
            repeats (int): repeat number for prediction
        Returns:
            result (dict): include 'boxes': np.ndarray: shape:[N,6], N: number of box,
                            matix element:[class, score, x_min, y_min, x_max, y_max]
        '''
        np_score_list, np_boxes_list = [], []
        for i in range(repeats):
            self.predictor.run()
            np_score_list.clear()
            np_boxes_list.clear()
            output_names = self.predictor.get_output_names()
            num_outs = int(len(output_names) / 2)
            for out_idx in range(num_outs):
                np_score_list.append(
                    self.predictor.get_output_handle(output_names[out_idx])
                        .copy_to_cpu())
                np_boxes_list.append(
                    self.predictor.get_output_handle(output_names[
                                                         out_idx + num_outs]).copy_to_cpu())
        result = dict(boxes=np_score_list, boxes_num=np_boxes_list)
        return result


def create_inputs(imgs, im_info):
    """generate input for different model type
    Args:
        imgs (list(numpy)): list of images (np.ndarray)
        im_info (list(dict)): list of image info
    Returns:
        inputs (dict): input of model
    """
    inputs = {}

    im_shape = []
    scale_factor = []
    if len(imgs) == 1:
        inputs['image'] = np.array((imgs[0],)).astype('float32')
        inputs['im_shape'] = np.array(
            (im_info[0]['im_shape'],)).astype('float32')
        inputs['scale_factor'] = np.array(
            (im_info[0]['scale_factor'],)).astype('float32')
        return inputs

    for e in im_info:
        im_shape.append(np.array((e['im_shape'],)).astype('float32'))
        scale_factor.append(np.array((e['scale_factor'],)).astype('float32'))

    inputs['im_shape'] = np.concatenate(im_shape, axis=0)
    inputs['scale_factor'] = np.concatenate(scale_factor, axis=0)

    imgs_shape = [[e.shape[1], e.shape[2]] for e in imgs]
    max_shape_h = max([e[0] for e in imgs_shape])
    max_shape_w = max([e[1] for e in imgs_shape])
    padding_imgs = []
    for img in imgs:
        im_c, im_h, im_w = img.shape[:]
        padding_im = np.zeros(
            (im_c, max_shape_h, max_shape_w), dtype=np.float32)
        padding_im[:, :im_h, :im_w] = img
        padding_imgs.append(padding_im)
    inputs['image'] = np.stack(padding_imgs, axis=0)
    return inputs


class PredictConfig():
    """set config of preprocess, postprocess and visualize
    Args:
        model_dir (str): root path of model.yml
    """

    def __init__(self, model_dir):
        # parsing Yaml config for Preprocess
        deploy_file = os.path.join(model_dir, 'infer_cfg.yml')
        with open(deploy_file) as f:
            yml_conf = yaml.safe_load(f)
        self.check_model(yml_conf)
        self.arch = yml_conf['arch']
        self.preprocess_infos = yml_conf['Preprocess']
        self.min_subgraph_size = yml_conf['min_subgraph_size']
        self.labels = yml_conf['label_list']
        self.mask = False
        self.use_dynamic_shape = yml_conf['use_dynamic_shape']
        if 'mask' in yml_conf:
            self.mask = yml_conf['mask']
        self.tracker = None
        if 'tracker' in yml_conf:
            self.tracker = yml_conf['tracker']
        if 'NMS' in yml_conf:
            self.nms = yml_conf['NMS']
        if 'fpn_stride' in yml_conf:
            self.fpn_stride = yml_conf['fpn_stride']
        if self.arch == 'RCNN' and yml_conf.get('export_onnx', False):
            logger.warning(
                'The RCNN export model is used for ONNX and it only supports batch_size = 1'
            )
        self.print_config()

    def check_model(self, yml_conf):
        """
        Raises:
            ValueError: loaded model not in supported model type 
        """
        for support_model in SUPPORT_MODELS:
            if support_model in yml_conf['arch']:
                return True
        raise ValueError("Unsupported arch: {}, expect {}".format(yml_conf[
                                                                      'arch'], SUPPORT_MODELS))

    def print_config(self):
        logger.debug('-----------  Model Configuration -----------')
        logger.debug('%s: %s' % ('Model Arch', self.arch))
        logger.debug('%s: ' % ('Transform Order'))
        for op_info in self.preprocess_infos:
            logger.debug('--%s: %s' % ('transform op', op_info['type']))
        logger.debug('--------------------------------------------')


def load_predictor(model_dir,
                   run_mode='paddle',
                   batch_size=1,
                   device='CPU',
                   min_subgraph_size=3,
                   use_dynamic_shape=False,
                   trt_min_shape=1,
                   trt_max_shape=1280,
                   trt_opt_shape=640,
                   trt_calib_mode=False,
                   cpu_threads=1,
                   enable_mkldnn=False,
                   enable_mkldnn_bfloat16=False,
                   delete_shuffle_pass=False):
    """set AnalysisConfig, generate AnalysisPredictor
    Args:
        model_dir (str): root path of __model__ and __params__
        device (str): Choose the device you want to run, it can be: CPU/GPU/XPU, default is CPU
        run_mode (str): mode of running(paddle/trt_fp32/trt_fp16/trt_int8)
        use_dynamic_shape (bool): use dynamic shape or not
        trt_min_shape (int): min shape for dynamic shape in trt
        trt_max_shape (int): max shape for dynamic shape in trt
        trt_opt_shape (int): opt shape for dynamic shape in trt
        trt_calib_mode (bool): If the model is produced by TRT offline quantitative
            calibration, trt_calib_mode need to set True
        delete_shuffle_pass (bool): whether to remove shuffle_channel_detect_pass in TensorRT. 
                                    Used by action model.
    Returns:
        predictor (PaddlePredictor): AnalysisPredictor
    Raises:
        ValueError: predict by TensorRT need device == 'GPU'.
    """
    if device != 'GPU' and run_mode != 'paddle':
        raise ValueError(
            "Predict by TensorRT mode: {}, expect device=='GPU', but device == {}"
                .format(run_mode, device))
    config = Config(
        os.path.join(model_dir, 'model.pdmodel'),
        os.path.join(model_dir, 'model.pdiparams'))
    if device == 'GPU':
        # initial GPU memory(M), device ID
        config.enable_use_gpu(200, 0)
        # optimize graph and fuse op
        config.switch_ir_optim(True)
    elif device == 'XPU':
        config.enable_lite_engine()
        config.enable_xpu(10 * 1024 * 1024)
    else:
        config.disable_gpu()
        config.set_cpu_math_library_num_threads(cpu_threads)
        if enable_mkldnn:
            try:
                # cache 10 different shapes for mkldnn to avoid memory leak
                config.set_mkldnn_cache_capacity(10)
                config.enable_mkldnn()
                if enable_mkldnn_bfloat16:
                    config.enable_mkldnn_bfloat16()
            except Exception as e:
                logger.error(
                    "The current environment does not support `mkldnn`, so disable mkldnn."
                )
                pass

    precision_map = {
        'trt_int8': Config.Precision.Int8,
        'trt_fp32': Config.Precision.Float32,
        'trt_fp16': Config.Precision.Half
    }
    if run_mode in precision_map.keys():
        config.enable_tensorrt_engine(
            workspace_size=(1 << 25) * batch_size,
            max_batch_size=batch_size,
            min_subgraph_size=min_subgraph_size,
            precision_mode=precision_map[run_mode],
            use_static=False,
            use_calib_mode=trt_calib_mode)

        if use_dynamic_shape:
            min_input_shape = {
                'image': [batch_size, 3, trt_min_shape, trt_min_shape]
            }
            max_input_shape = {
                'image': [batch_size, 3, trt_max_shape, trt_max_shape]
            }
            opt_input_shape = {
                'image': [batch_size, 3, trt_opt_shape, trt_opt_shape]
            }
            config.set_trt_dynamic_shape_info(min_input_shape, max_input_shape,
                                              opt_input_shape)
            logger.debug('trt set dynamic shape done!')

    # disable print log when predict
    config.disable_glog_info()
    # enable shared memory
    config.enable_memory_optim()
    # disable feed, fetch OP, needed by zero_copy_run
    config.switch_use_feed_fetch_ops(False)
    if delete_shuffle_pass:
        config.delete_pass("shuffle_channel_detect_pass")
    predictor = create_predictor(config)
    return predictor, config


def get_test_images(infer_dir, infer_img):
    """
    Get image path list in TEST mode
    """
    assert infer_img is not None or infer_dir is not None, \
        "--image_file or --image_dir should be set"
    assert infer_img is None or os.path.isfile(infer_img), \
        "{} is not a file".format(infer_img)
    assert infer_dir is None or os.path.isdir(infer_dir), \
        "{} is not a directory".format(infer_dir)

    # infer_img has a higher priority
    if infer_img and os.path.isfile(infer_img):
        return [infer_img]

    images = set()
    infer_dir = os.path.abspath(infer_dir)
    assert os.path.isdir(infer_dir), \
        "infer_dir {} is not a directory".format(infer_dir)
    exts = ['jpg', 'jpeg', 'png', 'bmp']
    exts += [ext.upper() for ext in exts]
    for ext in exts:
        images.update(glob.glob('{}/*.{}'.format(infer_dir, ext)))
    images = list(images)

    assert len(images) > 0, "no image found in {}".format(infer_dir)
    logger.info("Found {} inference images in total.".format(len(images)))

    return images


def visualize(image_list, result, labels, output_dir='output/', threshold=0.5):
    # visualize the predict result
    start_idx = 0
    for idx, image_file in enumerate(image_list):
        im_bboxes_num = result['boxes_num'][idx]
        im_results = {}
        if 'boxes' in result:
            im_results['boxes'] = result['boxes'][start_idx:start_idx +
                                                            im_bboxes_num, :]
        if 'masks' in result:
            im_results['masks'] = result['masks'][start_idx:start_idx +
                                                            im_bboxes_num, :]
        if 'segm' in result:
            im_results['segm'] = result['segm'][start_idx:start_idx +
                                                          im_bboxes_num, :]
        if 'label' in result:
            im_results['label'] = result['label'][start_idx:start_idx +
                                                            im_bboxes_num]
        if 'score' in result:
            im_results['score'] = result['score'][start_idx:start_idx +
                                                            im_bboxes_num]

        start_idx += im_bboxes_num
        im = visualize_box_mask(
            image_file, im_results, labels, threshold=threshold)
        img_name = os.path.split(image_file)[-1]
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        out_path = os.path.join(output_dir, img_name)
        im.save(out_path, quality=95)
        logger.info("save result to: " + out_path)


def generate_result(image_list, result, labels, threshold=0.5):
    json = []
    start_idx = 0
    for idx, image_file in enumerate(image_list):
        im_bboxes_num = result['boxes_num'][idx]
        im_results = {}
        if 'boxes' in result:
            im_results['boxes'] = result['boxes'][start_idx:start_idx +
                                                            im_bboxes_num, :]
        if 'masks' in result:
            im_results['masks'] = result['masks'][start_idx:start_idx +
                                                            im_bboxes_num, :]
        if 'segm' in result:
            im_results['segm'] = result['segm'][start_idx:start_idx +
                                                          im_bboxes_num, :]
        if 'label' in result:
            im_results['label'] = result['label'][start_idx:start_idx +
                                                            im_bboxes_num]
        if 'score' in result:
            im_results['score'] = result['score'][start_idx:start_idx +
                                                            im_bboxes_num]

        start_idx += im_bboxes_num
        im, distinguish = generate_box_mask(image_file, im_results, labels, threshold=threshold)
        json.append(distinguish)
    return json

def print_arguments(args):
    logger.debug('-----------  Running Arguments -----------')
    for arg, value in sorted(vars(args).items()):
        logger.debug('%s: %s' % (arg, value))
    logger.debug('------------------------------------------')


def main(args):
    deploy_file = os.path.join(args.model_dir, 'infer_cfg.yml')
    with open(deploy_file) as f:
        yml_conf = yaml.safe_load(f)
    arch = yml_conf['arch']
    detector_func = 'Detector'
    if arch == 'SOLOv2':
        detector_func = 'DetectorSOLOv2'
    elif arch == 'PicoDet':
        detector_func = 'DetectorPicoDet'

    detector = eval(detector_func)(
        args.model_dir,
        device=args.device,
        run_mode=args.run_mode,
        batch_size=args.batch_size,
        trt_min_shape=args.trt_min_shape,
        trt_max_shape=args.trt_max_shape,
        trt_opt_shape=args.trt_opt_shape,
        trt_calib_mode=args.trt_calib_mode,
        cpu_threads=args.cpu_threads,
        enable_mkldnn=args.enable_mkldnn,
        enable_mkldnn_bfloat16=args.enable_mkldnn_bfloat16,
        threshold=args.threshold,
        output_dir=args.output_dir)

    # predict from video file or camera video stream
    if args.video_file is not None or args.camera_id != -1:
        detector.predict_video(args.video_file, args.camera_id)
    else:
        # predict from image
        if args.image_dir is None and args.image_file is not None:
            assert args.batch_size == 1, "batch_size should be 1, when image_file is not None"
        img_list = get_test_images(args.image_dir, args.image_file)
        save_file = os.path.join(args.output_dir,
                                 'results.json') if args.save_results else None
        results, distinguishs = detector.predict_image(
            img_list, args.run_benchmark, repeats=100, save_file=save_file, visual=args.visual)
        if not args.run_benchmark:
            detector.det_times.info(average=True)
        else:
            mode = args.run_mode
            model_dir = args.model_dir
            model_info = {
                'model_name': model_dir.strip('/').split('/')[-1],
                'precision': mode.split('_')[-1]
            }
            bench_log(detector, img_list, model_info, name='DET')
    return distinguishs


def parse_arguments(mainRun=True):
    parser = argsparser()
    parser.add_argument(
        "--log_level",
        type=int,
        default=logging.INFO,
        help="log level.")
    parser.add_argument(
        "--visual",
        type=bool,
        default=False,
        help="visualize the predict result.")

    if mainRun:
        return parser.parse_args()
    else:
        inference_args_dict = {}
        for action in parser._actions:
            inference_args_dict[action.dest] = action.default
        return argparse.Namespace(**inference_args_dict)


class Predict:
    def __init__(self, **kwargs):
        paddle.enable_static()
        args = parse_arguments(mainRun=False)
        args.__dict__.update(**kwargs)

        args.device = args.device.upper()
        assert args.device in ['CPU', 'GPU', 'XPU'
                               ], "device should be CPU, GPU or XPU"
        assert not args.use_gpu, "use_gpu has been deprecated, please use --device"

        assert not (
                args.enable_mkldnn == False and args.enable_mkldnn_bfloat16 == True
        ), 'To enable mkldnn bfloat, please turn on both enable_mkldnn and enable_mkldnn_bfloat16'

        self.args = args

        logger.setLevel(self.args.log_level)

    def predict(self):
        """
        解析

        """

        return main(self.args)


if __name__ == '__main__':
    paddle.enable_static()
    parser = argsparser()
    args = parser.parse_args()
    print_arguments(args)
    args.device = args.device.upper()
    assert args.device in ['CPU', 'GPU', 'XPU'
                           ], "device should be CPU, GPU or XPU"
    assert not args.use_gpu, "use_gpu has been deprecated, please use --device"

    assert not (
            args.enable_mkldnn == False and args.enable_mkldnn_bfloat16 == True
    ), 'To enable mkldnn bfloat, please turn on both enable_mkldnn and enable_mkldnn_bfloat16'

    main(args)
