import sys
import os
from pathlib import Path
from PIL import Image
import concurrent.futures
from datetime import datetime
import subprocess
import tempfile
import shutil
import json
import ctypes
import ast

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QProgressBar,
    QPlainTextEdit, QFileDialog, QGroupBox, QMessageBox,
    QCheckBox, QComboBox, QSpinBox, QSlider
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSettings, QTimer
from PyQt5.QtGui import QTextCursor, QFont

# Windows 控制台优化
if sys.platform == 'win32':
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)


class CompressionWorker(QThread):
    """压缩工作线程"""
    progress_updated = pyqtSignal(int, str)
    file_done = pyqtSignal(dict)
    compression_finished = pyqtSignal(dict)
    log_message = pyqtSignal(str)

    def __init__(self, input_paths, output_dir, level=3, method='auto', keep_metadata=False, target_size=70):
        super().__init__()
        self.input_paths = [os.path.normpath(p) for p in input_paths]
        self.output_dir = os.path.normpath(output_dir)
        self.level = level
        self.method = method
        self.keep_metadata = keep_metadata
        self.target_size = target_size / 100.0
        self.is_running = True
        self.paused = False

        self.supported_formats = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG',
                                  '.bmp': 'BMP', '.gif': 'GIF', '.webp': 'WEBP'}

        self.tools = self.check_external_tools()

        # 增强的压缩级别设置 - 每个级别都有更明显的差异
        self.compression_levels = {
            1: {  # 轻度 - 高质量，轻度压缩
                'name': '轻度',
                'jpg_quality': 90,
                'png_compression': 3,  # PNG压缩级别 1-9
                'webp_quality': 90,
                'png_quant_quality': '80-90',  # pngquant质量范围
                'png_num_colors': 256,  # 颜色数
                'resize_threshold': 4000,  # 超过此尺寸才缩放
                'resize_factor': 1.0,  # 不缩放
                'gif_colors': 256,
                'optimize_extra': False,
                'strip_metadata': False
            },
            2: {  # 平衡 - 中等质量
                'name': '平衡',
                'jpg_quality': 80,
                'png_compression': 6,
                'webp_quality': 80,
                'png_quant_quality': '65-80',
                'png_num_colors': 128,
                'resize_threshold': 3000,
                'resize_factor': 0.9,  # 缩小10%
                'gif_colors': 128,
                'optimize_extra': True,
                'strip_metadata': False
            },
            3: {  # 强力 - 明显压缩
                'name': '强力',
                'jpg_quality': 70,
                'png_compression': 9,
                'webp_quality': 70,
                'png_quant_quality': '50-70',
                'png_num_colors': 64,
                'resize_threshold': 2500,
                'resize_factor': 0.8,  # 缩小20%
                'gif_colors': 64,
                'optimize_extra': True,
                'strip_metadata': True
            },
            4: {  # 极限 - 强烈压缩
                'name': '极限',
                'jpg_quality': 55,
                'png_compression': 9,
                'webp_quality': 55,
                'png_quant_quality': '30-55',
                'png_num_colors': 32,
                'resize_threshold': 2000,
                'resize_factor': 0.7,  # 缩小30%
                'gif_colors': 32,
                'optimize_extra': True,
                'strip_metadata': True
            },
            5: {  # 疯狂 - 最大压缩
                'name': '疯狂',
                'jpg_quality': 40,
                'png_compression': 9,
                'webp_quality': 40,
                'png_quant_quality': '20-40',
                'png_num_colors': 16,
                'resize_threshold': 1500,
                'resize_factor': 0.6,  # 缩小40%
                'gif_colors': 16,
                'optimize_extra': True,
                'strip_metadata': True
            }
        }

    def check_external_tools(self):
        tools = {}
        tool_checks = [
            ('pngquant', 'pngquant'), ('cjpeg', 'mozjpeg'),
            ('jpegoptim', 'jpegoptim'), ('optipng', 'optipng'),
            ('cwebp', 'cwebp'), ('gifsicle', 'gifsicle'),
            ('pngcrush', 'pngcrush'), ('jpegtran', 'jpegtran')
        ]

        for cmd, name in tool_checks:
            if shutil.which(cmd) or shutil.which(cmd + '.exe'):
                tools[name] = True
                self.log_message.emit(f"✓ 找到 {name}")

        return tools

    def toggle_pause(self):
        self.paused = not self.paused

    def stop(self):
        self.is_running = False

    def get_image_files(self, path):
        image_files = []
        path = Path(path)

        if path.is_file():
            return [str(path)] if path.suffix.lower() in self.supported_formats else []

        exclude_dirs = {'.git', '__pycache__', 'node_modules', 'System Volume Information'}
        try:
            for root, dirs, files in os.walk(str(path)):
                dirs[:] = [d for d in dirs if d not in exclude_dirs]
                for file in files:
                    if Path(file).suffix.lower() in self.supported_formats:
                        image_files.append(os.path.join(root, file))
        except Exception as e:
            self.log_message.emit(f"⚠ 读取文件夹失败: {str(e)}")

        return image_files

    def compress_image(self, input_file):
        try:
            params = self.compression_levels[self.level]
            input_path = Path(input_file)

            # 确定输出路径
            if len(self.input_paths) == 1 and os.path.isfile(self.input_paths[0]):
                base_path = Path(self.input_paths[0]).parent
            else:
                base_path = Path(self.input_paths[0])

            try:
                rel_path = input_path.relative_to(base_path)
            except ValueError:
                rel_path = input_path.name

            output_file = Path(self.output_dir) / rel_path
            output_file.parent.mkdir(parents=True, exist_ok=True)

            # 避免重名
            counter = 1
            while output_file.exists():
                output_file = output_file.with_name(f"{output_file.stem}_{counter}{output_file.suffix}")
                counter += 1

            original_size = os.path.getsize(input_file)

            # 获取图片信息
            with Image.open(input_file) as img:
                original_dimensions = img.size
                original_format = img.format

            suffix = input_path.suffix.lower()
            compressed = False
            method_used = "PIL"
            resize_applied = False

            # 1. 如果需要，先调整图片大小
            temp_file = None
            if max(original_dimensions) > params['resize_threshold'] and params['resize_factor'] < 1.0:
                temp_file = self.resize_image(input_file, params)
                if temp_file:
                    input_file = temp_file
                    resize_applied = True
                    method_used = "缩放"

            # 2. 根据格式选择最佳压缩方法
            if not compressed and suffix in ['.jpg', '.jpeg']:
                if 'mozjpeg' in self.tools and self.level >= 3:
                    method_used = "mozjpeg"
                    compressed = self.compress_with_mozjpeg_advanced(input_file, output_file, params)
                elif 'jpegoptim' in self.tools:
                    method_used = "jpegoptim"
                    compressed = self.compress_with_jpegoptim_advanced(input_file, output_file, params)
                elif 'jpegtran' in self.tools and self.level >= 4:
                    method_used = "jpegtran"
                    compressed = self.compress_with_jpegtran(input_file, output_file)

            if not compressed and suffix == '.png':
                if 'pngquant' in self.tools:
                    method_used = "pngquant"
                    compressed = self.compress_with_pngquant_advanced(input_file, output_file, params)
                elif 'pngcrush' in self.tools and self.level >= 4:
                    method_used = "pngcrush"
                    compressed = self.compress_with_pngcrush(input_file, output_file)
                elif 'optipng' in self.tools:
                    method_used = "optipng"
                    compressed = self.compress_with_optipng_advanced(input_file, output_file, params)

            if not compressed and suffix == '.webp' and 'cwebp' in self.tools:
                method_used = "cwebp"
                compressed = self.compress_with_cwebp_advanced(input_file, output_file, params)

            if not compressed and suffix == '.gif' and 'gifsicle' in self.tools:
                method_used = "gifsicle"
                compressed = self.compress_with_gifsicle(input_file, output_file, params)

            # 3. 最后用PIL
            if not compressed:
                method_used = "PIL" + ("+缩放" if resize_applied else "")
                compressed = self.compress_with_pil_advanced(input_file, output_file, params)

            # 4. 清理临时文件
            if temp_file and os.path.exists(temp_file):
                os.unlink(temp_file)

            if not compressed:
                raise Exception("压缩失败")

            compressed_size = os.path.getsize(output_file)

            # 如果压缩后反而变大，保留原文件
            if compressed_size > original_size:
                shutil.copy2(input_file if not temp_file else input_path, output_file)
                compressed_size = original_size
                saved = 0
            else:
                saved = original_size - compressed_size

            return {
                'success': True,
                'name': input_path.name,
                'original_size': original_size,
                'compressed_size': compressed_size,
                'saved': saved,
                'ratio': (saved / original_size * 100) if original_size > 0 else 0,
                'dimensions': original_dimensions,
                'new_dimensions': self.get_image_size(output_file) if resize_applied else original_dimensions,
                'format': original_format,
                'method': method_used,
                'resized': resize_applied
            }

        except Exception as e:
            return {
                'success': False,
                'name': Path(input_file).name,
                'error': str(e)
            }

    def get_image_size(self, image_path):
        """获取图片尺寸"""
        try:
            with Image.open(image_path) as img:
                return img.size
        except:
            return (0, 0)

    def resize_image(self, input_file, params):
        """调整图片大小"""
        try:
            with Image.open(input_file) as img:
                width, height = img.size
                new_width = int(width * params['resize_factor'])
                new_height = int(height * params['resize_factor'])

                # 使用Lanczos重采样获得更好质量
                img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

                # 保存到临时文件
                fd, temp_path = tempfile.mkstemp(suffix=Path(input_file).suffix)
                os.close(fd)

                # 根据原格式保存
                if img.format == 'JPEG':
                    img_resized.save(temp_path, 'JPEG', quality=95)
                elif img.format == 'PNG':
                    img_resized.save(temp_path, 'PNG', compress_level=1)
                else:
                    img_resized.save(temp_path)

                return temp_path
        except Exception as e:
            self.log_message.emit(f"缩放失败: {str(e)}")
            return None

    def compress_with_pil_advanced(self, input_file, output_file, params):
        """增强的PIL压缩"""
        try:
            with Image.open(input_file) as img:
                suffix = Path(input_file).suffix.lower()

                # 预处理：转换模式以获得更好的压缩
                if suffix in ['.jpg', '.jpeg']:
                    if img.mode in ['RGBA', 'P']:
                        img = img.convert('RGB')
                    # 使用optimize和progressive
                    img.save(output_file, 'JPEG',
                             quality=params['jpg_quality'],
                             optimize=True,
                             progressive=(self.level >= 3))
                elif suffix == '.png':
                    # PNG压缩：使用不同的策略
                    if self.level >= 4 and img.mode == 'RGBA':
                        # 极限模式下尝试减少颜色
                        img = img.convert('P', palette=Image.ADAPTIVE, colors=params['png_num_colors'])

                    img.save(output_file, 'PNG',
                             optimize=True,
                             compress_level=params['png_compression'])
                elif suffix == '.webp':
                    img.save(output_file, 'WEBP',
                             quality=params['webp_quality'],
                             method=6,  # 最慢但最好的压缩
                             lossless=False)
                else:
                    img.save(output_file)
                return True
        except Exception as e:
            self.log_message.emit(f"PIL压缩失败: {str(e)}")
            return False

    def compress_with_mozjpeg_advanced(self, input_file, output_file, params):
        """使用mozjpeg的高级压缩"""
        try:
            # 转换为PPM临时文件
            with Image.open(input_file) as img:
                if img.mode in ['RGBA', 'P']:
                    img = img.convert('RGB')
                with tempfile.NamedTemporaryFile(suffix='.ppm', delete=False) as tmp:
                    img.save(tmp.name, 'PPM')
                    tmp_path = tmp.name

            cjpeg = shutil.which('cjpeg') or shutil.which('cjpeg.exe')
            if not cjpeg:
                return False

            # mozjpeg参数：根据级别调整
            cmd = [cjpeg, '-quality', str(params['jpg_quality'])]

            # 高级选项
            if self.level >= 3:
                cmd.append('-optimize')
            if self.level >= 4:
                cmd.append('-progressive')
            if self.level >= 5:
                cmd.extend(['-sample', '2x2'])  # 色度抽样

            cmd.extend(['-outfile', str(output_file), tmp_path])

            result = subprocess.run(cmd, capture_output=True, shell=True)
            os.unlink(tmp_path)
            return result.returncode == 0
        except Exception as e:
            return False

    def compress_with_jpegoptim_advanced(self, input_file, output_file, params):
        """使用jpegoptim的高级压缩"""
        try:
            jpegoptim = shutil.which('jpegoptim') or shutil.which('jpegoptim.exe')
            if not jpegoptim:
                return False

            shutil.copy2(input_file, output_file)

            cmd = [jpegoptim, f'--max={params["jpg_quality"]}']

            if self.level >= 3:
                cmd.append('--strip-all')
            if self.level >= 4:
                cmd.append('--all-progressive')
            if self.level >= 5:
                cmd.append('--force')  # 强制优化

            cmd.append(str(output_file))

            result = subprocess.run(cmd, capture_output=True, shell=True)
            return result.returncode == 0
        except:
            return False

    def compress_with_pngquant_advanced(self, input_file, output_file, params):
        """使用pngquant的高级压缩"""
        try:
            pngquant = shutil.which('pngquant') or shutil.which('pngquant.exe')
            if not pngquant:
                return False

            cmd = [pngquant, f'--quality={params["png_quant_quality"]}']

            # 根据级别调整颜色数
            cmd.append(f'--colors={params["png_num_colors"]}')

            # 速度/质量平衡
            if self.level <= 2:
                cmd.append('--speed=3')  # 更快
            elif self.level == 3:
                cmd.append('--speed=1')  # 平衡
            else:
                cmd.append('--speed=1')  # 最慢但最好

            if self.level >= 4:
                cmd.append('--strip')  # 删除元数据

            cmd.extend(['--force', '--output', str(output_file), str(input_file)])

            result = subprocess.run(cmd, capture_output=True, shell=True)
            return result.returncode == 0
        except:
            return False

    def compress_with_optipng_advanced(self, input_file, output_file, params):
        """使用optipng的高级压缩"""
        try:
            optipng = shutil.which('optipng') or shutil.which('optipng.exe')
            if not optipng:
                return False

            shutil.copy2(input_file, output_file)

            # 优化级别：越高越慢但压缩率更好
            level = min(7, self.level + 4)  # 1-5级映射到5-7

            cmd = [optipng, f'-o{level}']

            if self.level >= 4:
                cmd.append('-strip')  # 删除元数据
                cmd.append('all')

            cmd.append(str(output_file))

            result = subprocess.run(cmd, capture_output=True, shell=True)
            return result.returncode == 0
        except:
            return False

    def compress_with_cwebp_advanced(self, input_file, output_file, params):
        """使用cwebp的高级压缩"""
        try:
            cwebp = shutil.which('cwebp') or shutil.which('cwebp.exe')
            if not cwebp:
                return False

            cmd = [cwebp, '-q', str(params['webp_quality'])]

            # 压缩方法：0-6，6最慢但最好
            method = min(6, self.level + 3)
            cmd.extend(['-m', str(method)])

            if self.level >= 3:
                cmd.append('-mt')  # 多线程
            if self.level >= 4:
                cmd.extend(['-pass', '10'])  # 更多分析pass
            if self.level >= 5:
                cmd.append('-sharp_yuv')  # 更好的YUV转换

            cmd.extend(['-o', str(output_file), str(input_file)])

            result = subprocess.run(cmd, capture_output=True, shell=True)
            return result.returncode == 0
        except:
            return False

    def compress_with_pngcrush(self, input_file, output_file):
        """使用pngcrush压缩"""
        try:
            pngcrush = shutil.which('pngcrush') or shutil.which('pngcrush.exe')
            if not pngcrush:
                return False

            # pngcrush有多个算法，尝试不同的brute force选项
            cmd = [pngcrush, '-brute', '-reduce', '-ow', str(input_file), str(output_file)]

            result = subprocess.run(cmd, capture_output=True, shell=True)
            return result.returncode == 0
        except:
            return False

    def compress_with_jpegtran(self, input_file, output_file):
        """使用jpegtran进行无损压缩"""
        try:
            jpegtran = shutil.which('jpegtran') or shutil.which('jpegtran.exe')
            if not jpegtran:
                return False

            cmd = [jpegtran, '-copy', 'none', '-optimize', '-progressive',
                   '-outfile', str(output_file), str(input_file)]

            result = subprocess.run(cmd, capture_output=True, shell=True)
            return result.returncode == 0
        except:
            return False

    def compress_with_gifsicle(self, input_file, output_file, params):
        """使用gifsicle压缩GIF"""
        try:
            gifsicle = shutil.which('gifsicle') or shutil.which('gifsicle.exe')
            if not gifsicle:
                return False

            cmd = [gifsicle, '--optimize=3']  # 最大优化

            if self.level >= 3:
                cmd.append('--colors=%d' % params['gif_colors'])
            if self.level >= 4:
                cmd.append('--lossy=80')  # 有损压缩

            cmd.extend(['--output', str(output_file), str(input_file)])

            result = subprocess.run(cmd, capture_output=True, shell=True)
            return result.returncode == 0
        except:
            return False

    def run(self):
        try:
            all_images = []
            self.log_message.emit("🔍 正在扫描文件...")

            for input_path in self.input_paths:
                if os.path.isdir(input_path):
                    images = self.get_image_files(input_path)
                    all_images.extend(images)
                    self.log_message.emit(f"📁 {os.path.basename(input_path)}: {len(images)} 个图片")
                else:
                    all_images.append(input_path)

            if not all_images:
                self.log_message.emit("⚠ 未找到图片文件")
                self.compression_finished.emit({'success': [], 'failed': []})
                return

            total = len(all_images)
            params = self.compression_levels[self.level]
            self.log_message.emit(f"📊 共 {total} 个图片 - {params['name']}模式")
            if params['resize_factor'] < 1.0:
                self.log_message.emit(
                    f"📏 自动缩放: 大于{params['resize_threshold']}px的图片将缩小至{int(params['resize_factor'] * 100)}%")

            results = {'success': [], 'failed': [], 'total_original': 0, 'total_compressed': 0, 'total_saved': 0}

            # 使用线程池，但限制并发数以避免资源竞争
            max_workers = min(2 if self.level >= 4 else 4, os.cpu_count() or 2)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(self.compress_image, img): img for img in all_images}
                completed = 0

                for future in concurrent.futures.as_completed(futures):
                    if not self.is_running:
                        break

                    while self.paused and self.is_running:
                        self.msleep(100)

                    result = future.result()
                    completed += 1

                    if result['success']:
                        results['success'].append(result)
                        results['total_original'] += result['original_size']
                        results['total_compressed'] += result['compressed_size']
                        results['total_saved'] += result['saved']

                        self.file_done.emit(result)
                        self.progress_updated.emit(int(completed / total * 100), result['name'])

                        if result['saved'] > 0:
                            ratio_info = f" -{result['ratio']:.1f}%"
                            resize_info = " [已缩放]" if result.get('resized', False) else ""
                            self.log_message.emit(
                                f"✅ {result['name'][:30]:<30} "
                                f"{self.format_size(result['original_size']):>8} → "
                                f"{self.format_size(result['compressed_size']):>8} "
                                f"{ratio_info}{resize_info} [{result['method']}]"
                            )
                    else:
                        results['failed'].append(result)
                        self.log_message.emit(f"❌ {result['name']} - {result['error']}")

            self.compression_finished.emit(results)

        except Exception as e:
            self.log_message.emit(f"🔥 错误: {str(e)}")
            self.compression_finished.emit({'success': [], 'failed': []})

    @staticmethod
    def format_size(size):
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f}MB"
        return f"{size / (1024 * 1024 * 1024):.2f}GB"


class Settings:
    """设置管理"""

    def __init__(self):
        self.settings = QSettings("ImageCompressor", "Advanced")

    def get(self, key, default=None):
        return self.settings.value(key, default)

    def set(self, key, value):
        self.settings.setValue(key, value)


class ImageCompressor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.input_paths = []
        self.worker = None
        self.init_ui()
        self.load_settings()

    def init_ui(self):
        self.setWindowTitle("图片压缩工具 - 高级版")
        self.resize(950, 720)

        # 设置样式
        self.setStyleSheet("""
            QMainWindow { background-color: #f5f5f5; }
            QGroupBox { 
                font-weight: bold; border: 1px solid #ddd; 
                border-radius: 5px; margin-top: 10px; padding-top: 10px;
                background-color: white;
            }
            QPushButton { 
                background-color: #2196F3; color: white; border: none;
                padding: 8px 16px; border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover { background-color: #1976D2; }
            QPushButton:disabled { background-color: #bdbdbd; }
            QProgressBar { border: none; border-radius: 3px; height: 20px; }
            QProgressBar::chunk { background-color: #4CAF50; border-radius: 3px; }
            QSlider::groove:horizontal { height: 6px; background: #ddd; border-radius: 3px; }
            QSlider::handle:horizontal { background: #2196F3; width: 18px; margin: -5px 0; border-radius: 9px; }
        """)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)

        # 文件选择区域
        file_group = QGroupBox("📁 文件选择")
        file_layout = QHBoxLayout()

        self.file_label = QLabel("未选择文件")
        self.file_label.setStyleSheet("padding: 5px; background-color: #f0f0f0; border-radius: 3px;")
        file_layout.addWidget(self.file_label, 1)

        self.add_btn = QPushButton("添加文件")
        self.add_btn.clicked.connect(self.add_files)
        file_layout.addWidget(self.add_btn)

        self.folder_btn = QPushButton("添加文件夹")
        self.folder_btn.clicked.connect(self.add_folder)
        file_layout.addWidget(self.folder_btn)

        self.clear_btn = QPushButton("清空")
        self.clear_btn.setStyleSheet("background-color: #ff9800;")
        self.clear_btn.clicked.connect(self.clear_files)
        file_layout.addWidget(self.clear_btn)

        file_group.setLayout(file_layout)
        layout.addWidget(file_group)

        # 压缩设置
        setting_group = QGroupBox("⚙ 压缩设置")
        setting_layout = QVBoxLayout()

        # 压缩级别 - 使用滑块
        level_layout = QHBoxLayout()
        level_layout.addWidget(QLabel("压缩强度:"))

        self.level_slider = QSlider(Qt.Horizontal)
        self.level_slider.setRange(1, 5)
        self.level_slider.setValue(3)
        self.level_slider.setTickPosition(QSlider.TicksBelow)
        self.level_slider.setTickInterval(1)
        self.level_slider.setPageStep(1)
        self.level_slider.valueChanged.connect(self.on_level_changed)
        level_layout.addWidget(self.level_slider, 2)

        self.level_label = QLabel("强力")
        self.level_label.setMinimumWidth(60)
        self.level_label.setStyleSheet("color: #2196F3; font-weight: bold;")
        level_layout.addWidget(self.level_label)

        level_layout.addStretch()
        setting_layout.addLayout(level_layout)

        # 压缩效果预览
        effect_layout = QHBoxLayout()
        effect_layout.addWidget(QLabel("预期效果:"))
        self.effect_label = QLabel("质量70% | PNG压缩9级 | 可能缩放20% | 删除元数据")
        self.effect_label.setStyleSheet("color: #666; font-size: 10pt;")
        effect_layout.addWidget(self.effect_label)
        effect_layout.addStretch()
        setting_layout.addLayout(effect_layout)

        # 目标大小和引擎
        options_layout = QHBoxLayout()

        options_layout.addWidget(QLabel("目标质量:"))
        self.target_spin = QSpinBox()
        self.target_spin.setRange(10, 95)
        self.target_spin.setValue(70)
        self.target_spin.setSuffix("%")
        options_layout.addWidget(self.target_spin)

        options_layout.addWidget(QLabel("引擎:"))
        self.method_combo = QComboBox()
        self.method_combo.addItems(["智能自动", "mozjpeg", "pngquant", "cwebp", "PIL内置"])
        options_layout.addWidget(self.method_combo)

        self.keep_meta = QCheckBox("保留元数据")
        options_layout.addWidget(self.keep_meta)

        options_layout.addStretch()
        setting_layout.addLayout(options_layout)

        # 高级选项
        advanced_btn = QPushButton("⚡ 高级选项")
        advanced_btn.setCheckable(True)
        advanced_btn.setStyleSheet("background-color: #9C27B0;")
        advanced_btn.toggled.connect(self.toggle_advanced)
        setting_layout.addWidget(advanced_btn)

        # 高级选项面板
        self.advanced_widget = QWidget()
        self.advanced_widget.setVisible(False)
        advanced_layout = QHBoxLayout(self.advanced_widget)
        advanced_layout.setContentsMargins(0, 5, 0, 0)

        self.resize_check = QCheckBox("自动缩放大图")
        self.resize_check.setChecked(True)
        advanced_layout.addWidget(self.resize_check)

        advanced_layout.addWidget(QLabel("缩放阈值:"))
        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(1000, 5000)
        self.threshold_spin.setValue(2500)
        self.threshold_spin.setSuffix("px")
        advanced_layout.addWidget(self.threshold_spin)

        self.multipass_check = QCheckBox("多遍优化(更慢但更好)")
        self.multipass_check.setChecked(True)
        advanced_layout.addWidget(self.multipass_check)

        advanced_layout.addStretch()
        setting_layout.addWidget(self.advanced_widget)

        # 输出目录
        output_layout = QHBoxLayout()
        output_layout.addWidget(QLabel("输出到:"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("自动在源目录创建 compressed 文件夹")
        output_layout.addWidget(self.output_edit, 1)

        self.output_btn = QPushButton("选择")
        self.output_btn.setMaximumWidth(60)
        self.output_btn.clicked.connect(self.select_output)
        output_layout.addWidget(self.output_btn)

        setting_layout.addLayout(output_layout)
        setting_group.setLayout(setting_layout)
        layout.addWidget(setting_group)

        # 进度条
        self.progress = QProgressBar()
        layout.addWidget(self.progress)

        # 控制按钮
        btn_layout = QHBoxLayout()

        self.start_btn = QPushButton("🚀 开始压缩")
        self.start_btn.setMinimumHeight(40)
        self.start_btn.setStyleSheet("background-color: #4CAF50; font-size: 16px;")
        self.start_btn.clicked.connect(self.start_compression)
        btn_layout.addWidget(self.start_btn, 3)

        self.pause_btn = QPushButton("⏸️ 暂停")
        self.pause_btn.setEnabled(False)
        self.pause_btn.setStyleSheet("background-color: #FF9800;")
        self.pause_btn.clicked.connect(self.toggle_pause)
        btn_layout.addWidget(self.pause_btn, 1)

        self.stop_btn = QPushButton("⏹️ 停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("background-color: #F44336;")
        self.stop_btn.clicked.connect(self.stop_compression)
        btn_layout.addWidget(self.stop_btn, 1)

        layout.addLayout(btn_layout)

        # 日志区域
        log_group = QGroupBox("📝 日志")
        log_layout = QVBoxLayout()

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(500)
        self.log_text.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_text)

        log_group.setLayout(log_layout)
        layout.addWidget(log_group, 1)

        # 状态栏
        self.status_label = QLabel("✨ 就绪")
        self.statusBar().addWidget(self.status_label)

        QTimer.singleShot(500, self.check_tools)
        self.on_level_changed(3)

    def toggle_advanced(self, checked):
        self.advanced_widget.setVisible(checked)

    def check_tools(self):
        tools = []
        for cmd in ['pngquant', 'cjpeg', 'jpegoptim', 'optipng', 'cwebp', 'gifsicle']:
            if shutil.which(cmd) or shutil.which(cmd + '.exe'):
                tools.append(cmd)
        if tools:
            self.statusBar().showMessage(f"🔧 外部工具: {', '.join(tools)}")

    def load_settings(self):
        level = self.settings.get("level", 3)
        self.level_slider.setValue(int(level))

        self.method_combo.setCurrentIndex(self.settings.get("method", 0))
        self.keep_meta.setChecked(self.settings.get("keep_metadata", "false") == "true")
        self.target_spin.setValue(int(self.settings.get("target_size", 70)))

        output = self.settings.get("output_dir", "")
        if output and os.path.exists(output):
            self.output_edit.setText(output)

    def save_settings(self):
        self.settings.set("level", self.level_slider.value())
        self.settings.set("method", self.method_combo.currentIndex())
        self.settings.set("keep_metadata", "true" if self.keep_meta.isChecked() else "false")
        self.settings.set("target_size", self.target_spin.value())
        if self.output_edit.text():
            self.settings.set("output_dir", self.output_edit.text())

    def on_level_changed(self, level):
        names = ["轻度", "平衡", "强力", "极限", "疯狂"]
        effects = [
            "质量90% | 轻度PNG压缩 | 不缩放 | 保留元数据",
            "质量80% | 中等PNG压缩 | 缩小10% | 保留元数据",
            "质量70% | PNG压缩9级 | 缩小20% | 删除元数据",
            "质量55% | 减少颜色 | 缩小30% | 极限优化",
            "质量40% | 16色 | 缩小40% | 暴力压缩"
        ]
        self.level_label.setText(names[level - 1])
        self.effect_label.setText(effects[level - 1])

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择图片", "", "图片 (*.jpg *.jpeg *.png *.gif *.webp)"
        )
        if files:
            self.add_paths(files)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder:
            self.add_paths([folder])

    def add_paths(self, paths):
        added = 0
        for p in paths:
            p = os.path.normpath(p)
            if p not in self.input_paths:
                self.input_paths.append(p)
                added += 1

        if added > 0:
            self.update_file_label()
            self.log(f"📥 已添加 {added} 个项目")

    def clear_files(self):
        if self.input_paths:
            self.input_paths.clear()
            self.update_file_label()
            self.log("🧹 已清空列表")

    def update_file_label(self):
        files = sum(1 for p in self.input_paths if os.path.isfile(p))
        folders = sum(1 for p in self.input_paths if os.path.isdir(p))
        self.file_label.setText(f"📄 {files}个文件  📁 {folders}个文件夹")

    def select_output(self):
        folder = QFileDialog.getExistingDirectory(self, "选择输出文件夹")
        if folder:
            self.output_edit.setText(folder)
            self.save_settings()

    def log(self, msg, level="info"):
        time = datetime.now().strftime("%H:%M:%S")
        self.log_text.appendPlainText(f"[{time}] {msg}")
        self.log_text.moveCursor(QTextCursor.End)
        QApplication.processEvents()

    def toggle_pause(self):
        if self.worker:
            self.worker.toggle_pause()
            self.pause_btn.setText("▶️ 继续" if self.worker.paused else "⏸️ 暂停")
            self.log("⏸️ 已暂停" if self.worker.paused else "▶️ 继续")

    def stop_compression(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.log("🛑 正在停止...")

    def start_compression(self):
        if not self.input_paths:
            QMessageBox.warning(self, "提示", "请先选择文件")
            return

        # 输出目录
        output = self.output_edit.text()
        if not output:
            first = self.input_paths[0]
            base = os.path.dirname(first) if os.path.isfile(first) else first
            output = os.path.join(base, "compressed")
            self.output_edit.setText(output)

        try:
            os.makedirs(output, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法创建输出目录:\n{str(e)}")
            return

        # 获取设置
        level = self.level_slider.value()
        method_map = ["auto", "mozjpeg", "pngquant", "webp", "pil"]
        method = method_map[self.method_combo.currentIndex()]

        self.save_settings()

        # 创建工作线程
        self.worker = CompressionWorker(
            self.input_paths, output, level, method,
            self.keep_meta.isChecked(), self.target_spin.value()
        )

        self.worker.progress_updated.connect(lambda v, f: self.progress.setValue(v))
        self.worker.file_done.connect(lambda r: self.status_label.setText(f"处理: {r['name']}"))
        self.worker.compression_finished.connect(self.on_finished)
        self.worker.log_message.connect(lambda m: self.log(m))

        # 更新界面
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.add_btn.setEnabled(False)
        self.folder_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)
        self.progress.setValue(0)

        self.log("=" * 50)
        self.log(f"🚀 开始压缩 - {['轻度', '平衡', '强力', '极限', '疯狂'][level - 1]}模式")
        self.log(f"📂 输出: {output}")

        self.worker.start()

    def on_finished(self, results):
        # 恢复界面
        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.add_btn.setEnabled(True)
        self.folder_btn.setEnabled(True)
        self.clear_btn.setEnabled(True)
        self.pause_btn.setText("⏸️ 暂停")

        success = len(results['success'])
        failed = len(results['failed'])

        self.log("=" * 50)
        if success > 0:
            saved = results['total_saved']
            ratio = (saved / results['total_original'] * 100) if results['total_original'] > 0 else 0
            self.log(f"✅ 成功: {success} 个")
            self.log(f"💾 节省: {self.format_size(saved)} ({ratio:.1f}%)")

            # 计算平均压缩率
            if results['total_original'] > 0:
                avg_ratio = (1 - results['total_compressed'] / results['total_original']) * 100
                self.log(f"📊 平均压缩率: {avg_ratio:.1f}%")

            self.status_label.setText(f"✨ 完成 - 节省 {self.format_size(saved)}")
            self.progress.setValue(100)
        else:
            self.log("❌ 没有成功压缩的文件")
            self.status_label.setText("❌ 处理失败")

        if failed > 0:
            self.log(f"❌ 失败: {failed} 个")

        self.log("=" * 50)

    @staticmethod
    def format_size(size):
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f}MB"
        return f"{size / (1024 * 1024 * 1024):.2f}GB"

    def closeEvent(self, event):
        # 保存窗口尺寸为字符串，避免类型问题
        width = self.width()
        height = self.height()
        self.settings.set("window_size", f"({width}, {height})")

        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(self, "确认", "压缩正在进行，确定退出？",
                                         QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.worker.stop()
                self.worker.wait()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setApplicationName("图片压缩工具")

    window = ImageCompressor()

    # 恢复窗口大小 - 修复版本
    size = window.settings.get("window_size")
    if size:
        try:
            # 如果size是字符串，尝试解析
            if isinstance(size, str):
                # 移除括号并分割
                size_str = size.strip('()')
                w, h = map(int, size_str.split(','))
                window.resize(w, h)
            elif isinstance(size, (tuple, list)) and len(size) == 2:
                # 将两个数字作为单独参数传入
                window.resize(int(size[0]), int(size[1]))
        except (ValueError, TypeError, AttributeError) as e:
            print(f"恢复窗口大小失败: {e}")
            # 使用默认大小
            window.resize(950, 720)

    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()