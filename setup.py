#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import print_function
from setuptools import setup, find_packages
# from distutils.core import setup
import os
import stat
import shutil
import platform
import sys
import site
import glob


# -- file paths --
long_description="""SRNET: Python implementation of SRNET"""
setup(
    name='srnet',
    version='100.100.100',
    description='A python implementation of SRNET',
    long_description=long_description,
    url='https://github.com/gauenk/cola_net',
    author='Kent Gauen',
    author_email='gauenk@purdue.edu',
    license='MIT',
    keywords='burst denoising, non-local search, video denoising, neural network',
    install_requires=['numpy','torch','flake8'],
    package_dir={"": "lib"},
    packages=find_packages("lib"),
)
