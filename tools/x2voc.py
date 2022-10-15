#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import os


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--data_dir', type=str, required=True)
    parser.add_argument('--out_filename', type=str, default='all_list.txt')
    args = parser.parse_args()

    if not os.path.exists(args.data_dir):
        os.makedirs(args.data_dir)

    out_file = os.path.join(args.data_dir, args.out_filename)

    with open(out_file, 'w') as file:
        # 读取图片
        for img_file in os.listdir(args.data_dir + '/images'):
            img_name = os.path.splitext(img_file)
            img_label = img_name[0]
            # print(img_label)
            if img_file.split('.')[
                -1] not in ['bmp', 'jpg', 'jpeg', 'png', 'JPEG', 'JPG', 'PNG']:
                continue
            file.write('images/' + img_label + img_name[1] + ' annotations/' + img_label + '.xml\n')


if __name__ == '__main__':
    main()
