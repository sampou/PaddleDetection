#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import os


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--image_dir', type=str)
    parser.add_argument('--output_dir', type=str, default='./out')
    parser.add_argument('--init_num', type=int, default=1)
    args = parser.parse_args()

    try:
        assert os.path.exists(args.image_dir)
    except AssertionError as e:
        print('image_dir folder does not exist!')
        os._exit(0)

    output_dir = args.output_dir
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    cur_num = args.init_num
    # 读取图片
    for img_file in os.listdir(args.image_dir):
        img_name = os.path.splitext(img_file)
        # print(img_name)
        if img_file.split('.')[
            -1] not in ['bmp', 'jpg', 'jpeg', 'png', 'JPEG', 'JPG', 'PNG']:
            continue
        os.rename(args.image_dir + '/' + img_file, output_dir + '/1-' + str(cur_num) + img_name[1])
        # print('{0}, {1}'.format(args.image_dir + '/' + img_file, output_dir + '/1-' + str(cur_num) + img_name[1]))
        cur_num += 1


if __name__ == '__main__':
    main()
