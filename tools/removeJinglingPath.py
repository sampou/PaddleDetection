#!/usr/bin/env python
# -*- coding: utf-8 -*-

try:
    import xml.etree.cElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET

import argparse
import os


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--xml_dir', type=str, required=True)
    parser.add_argument('--output_dir', type=str, default='./out')
    parser.add_argument('--new_file', action='store_true', default=False)
    args = parser.parse_args()

    try:
        assert os.path.exists(args.xml_dir)
    except AssertionError as e:
        print('xml_dir folder does not exist!')
        os._exit(0)

    new_file = args.new_file
    if new_file:
        output_dir = args.output_dir
    else:
        output_dir = args.xml_dir

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # 读取图片
    for xml_file in os.listdir(args.xml_dir):
        tree = ET.parse(os.path.join(args.xml_dir, xml_file))
        root = tree.getroot()
        path_ele = root.find('path')
        if not path_ele:
            root.remove(path_ele)
            tree.write(os.path.join(output_dir, xml_file))


if __name__ == '__main__':
    main()
