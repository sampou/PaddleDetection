#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging

from deploy.python.predict import Predict

if __name__ == '__main__':
    pre = Predict(model_dir='F:\pycharm_work\PaddleDetection\output\yolov3_mobilenet_v3_travel_card',
                  image_dir='F:\项目\健康码识别\行程码\素材',
                  log_level=logging.DEBUG)
    pre.predict()
