#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging

from deploy.python.predict import Predict

if __name__ == '__main__':
    pre = Predict(model_dir=r'F:\pycharm_work\PaddleDetection\output\yolov3_mobilenet_v3_travel_card',
                  image_file=r'F:\项目\健康码识别\行程码\素材\1.jpg',
                  log_level=logging.DEBUG)
    print(pre.predict())
