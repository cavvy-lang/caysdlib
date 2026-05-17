#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SDL2 自动下载脚本
功能：自动下载SDL2指定版本（或全部版本）并将库文件整理到Libs文件夹
时间复杂度: O(n) 其中n为版本数量
磁盘IO复杂度: O(m) 其中m为下载文件总大小
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# SDL2 GitHub Releases API
SDL2_REPO_OWNER = "libsdl-org"
SDL2_REPO_NAME = "SDL"
GITHUB_API_BASE = "https://api.github.com"

# Windows开发库文件名模式
SDL2_DEVEL_PATTERN = re.compile(r'SDL2-devel-(\d+\.\d+\.\d+)-VC\.zip', re.IGNORECASE)


class AtomicFileWriter:
    """
    原子文件写入器
    实现四层防护：临时文件 -> 验证完整性 -> 备份原文件 -> 重命名替换
    """
    
    def __init__(self, target_path: Path, max_backups: int = 3):
        self.target_path = Path(target_path)
        self.max_backups = max_backups
        self.temp_path: Optional[Path] = None
        self.backup_path: Optional[Path] = None
    
    def __enter__(self) -> 'AtomicFileWriter':
        # 创建临时文件
        self.temp_path = Path(tempfile.mktemp(
            prefix=f".tmp_{self.target_path.name}.",
            dir=self.target_path.parent
        ))
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            # 发生异常，清理临时文件
            if self.temp_path and self.temp_path.exists():
                try:
                    self.temp_path.unlink()
                except OSError:
                    pass
            return False
        
        # 验证临时文件
        if not self.temp_path or not self.temp_path.exists():
            raise RuntimeError(f"临时文件不存在: {self.temp_path}")
        
        if self.temp_path.stat().st_size == 0:
            self.temp_path.unlink()
            raise RuntimeError("临时文件为空，写入失败")
        
        # 备份原文件
        if self.target_path.exists():
            self._create_backup()
        
        # 原子替换
        try:
            self.temp_path.replace(self.target_path)
            logger.debug(f"原子写入成功: {self.target_path}")
        except OSError as e:
            # 回滚
            self._rollback()
            raise RuntimeError(f"原子替换失败: {e}")
        
        return True
    
    def _create_backup(self):
        """创建备份文件，保留最近N个版本"""
        backup_dir = self.target_path.parent / ".backups"
        backup_dir.mkdir(exist_ok=True)
        
        # 清理旧备份
        backups = sorted(backup_dir.glob(f"{self.target_path.name}.*"))
        while len(backups) >= self.max_backups:
            try:
                backups[0].unlink()
                backups = backups[1:]
            except OSError:
                break
        
        # 创建新备份
        timestamp = int(os.path.getmtime(self.target_path))
        self.backup_path = backup_dir / f"{self.target_path.name}.{timestamp}.bak"
        shutil.copy2(self.target_path, self.backup_path)
        logger.debug(f"备份创建: {self.backup_path}")
    
    def _rollback(self):
        """回滚到备份"""
        if self.backup_path and self.backup_path.exists():
            try:
                shutil.copy2(self.backup_path, self.target_path)
                logger.warning(f"已回滚到备份: {self.backup_path}")
            except OSError as e:
                logger.error(f"回滚失败: {e}")
    
    def write(self, data: bytes):
        """写入数据到临时文件"""
        with open(self.temp_path, 'wb') as f:
            f.write(data)
    
    def write_text(self, text: str, encoding: str = 'utf-8'):
        """写入文本到临时文件"""
        self.write(text.encode(encoding))


class SDL2Downloader:
    """
    SDL2 下载器
    支持从GitHub Releases下载Windows开发库
    """
    
    def __init__(self, output_dir: Path, proxy: Optional[str] = None):
        self.output_dir = Path(output_dir).resolve()
        self.libs_dir = self.output_dir / "Libs"
        self.downloads_dir = self.output_dir / "Downloads"
        self.proxy = proxy
        
        # 创建目录
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.libs_dir.mkdir(exist_ok=True)
        self.downloads_dir.mkdir(exist_ok=True)
        
        # 配置代理
        self.opener = self._create_opener()
    
    def _create_opener(self) -> urllib.request.OpenerDirector:
        """创建带代理支持的URL opener"""
        handlers = []
        
        if self.proxy:
            proxy_handler = urllib.request.ProxyHandler({
                'http': self.proxy,
                'https': self.proxy
            })
            handlers.append(proxy_handler)
        
        # 添加User-Agent
        headers = [('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0')]
        
        opener = urllib.request.build_opener(*handlers)
        opener.addheaders = headers
        return opener
    
    def _api_request(self, endpoint: str) -> dict:
        """
        发送GitHub API请求
        时间复杂度: O(1) 网络IO
        """
        url = urljoin(GITHUB_API_BASE, endpoint)
        logger.debug(f"API请求: {url}")
        
        try:
            with self.opener.open(url, timeout=30) as response:
                data = response.read()
                return json.loads(data.decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise RuntimeError("GitHub API速率限制 exceeded. 请稍后重试或配置token")
            raise RuntimeError(f"API请求失败: {e.code} {e.reason}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络请求失败: {e.reason}")
    
    def get_all_releases(self) -> List[Dict]:
        """
        获取所有发布版本
        时间复杂度: O(1) 单次API调用
        """
        endpoint = f"/repos/{SDL2_REPO_OWNER}/{SDL2_REPO_NAME}/releases"
        releases = self._api_request(endpoint)
        
        # 过滤SDL2版本（SDL3以3.x开头）
        sdl2_releases = []
        for release in releases:
            tag = release.get('tag_name', '')
            # SDL2版本以2.开头
            if tag.startswith('release-2.'):
                sdl2_releases.append(release)
        
        logger.info(f"找到 {len(sdl2_releases)} 个SDL2版本")
        return sdl2_releases
    
    def find_windows_devel_asset(self, release: Dict) -> Optional[Dict]:
        """
        查找Windows开发库资源
        时间复杂度: O(a) 其中a为资源数量
        """
        assets = release.get('assets', [])
        
        for asset in assets:
            name = asset.get('name', '')
            if SDL2_DEVEL_PATTERN.match(name):
                return asset
        
        return None
    
    def download_file(self, url: str, target_path: Path, expected_size: Optional[int] = None) -> bool:
        """
        下载文件到指定路径
        时间复杂度: O(s) 其中s为文件大小
        磁盘IO复杂度: O(s)
        """
        logger.info(f"下载: {url}")
        
        try:
            with self.opener.open(url, timeout=60) as response:
                total_size = int(response.headers.get('Content-Length', 0))
                
                # 分块下载
                chunk_size = 8192
                downloaded = 0
                chunks = []
                
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    
                    if total_size > 0:
                        progress = (downloaded / total_size) * 100
                        if downloaded % (chunk_size * 100) == 0:
                            logger.info(f"下载进度: {progress:.1f}%")
                
                data = b''.join(chunks)
                
                # 验证大小
                if expected_size and len(data) != expected_size:
                    logger.warning(f"文件大小不匹配: 期望 {expected_size}, 实际 {len(data)}")
                
                # 原子写入
                with AtomicFileWriter(target_path) as writer:
                    writer.write(data)
                
                logger.info(f"下载完成: {target_path}")
                return True
                
        except Exception as e:
            logger.error(f"下载失败: {e}")
            return False
    
    def extract_and_organize(self, zip_path: Path, version: str) -> bool:
        """
        解压并整理库文件到Libs目录
        时间复杂度: O(f) 其中f为压缩包内文件数
        磁盘IO复杂度: O(f)
        """
        version_dir = self.libs_dir / f"SDL2-{version}"
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                # 检查压缩包内容
                namelist = zf.namelist()
                logger.debug(f"压缩包内容: {len(namelist)} 个文件")
                
                # 清理已存在的目录
                if version_dir.exists():
                    logger.warning(f"清理已存在的目录: {version_dir}")
                    shutil.rmtree(version_dir)
                
                version_dir.mkdir(parents=True, exist_ok=True)
                
                # 解压所有文件
                for member in namelist:
                    # 跳过macOS元数据文件
                    if '__MACOSX' in member:
                        continue
                    
                    zf.extract(member, version_dir)
                
                # 查找实际的SDL2目录（通常在解压后的子目录中）
                subdirs = [d for d in version_dir.iterdir() if d.is_dir()]
                if len(subdirs) == 1 and subdirs[0].name.startswith('SDL2'):
                    # 移动内容到版本目录根
                    sdl2_subdir = subdirs[0]
                    for item in sdl2_subdir.iterdir():
                        target = version_dir / item.name
                        if target.exists():
                            if target.is_dir():
                                shutil.rmtree(target)
                            else:
                                target.unlink()
                        shutil.move(str(item), str(target))
                    sdl2_subdir.rmdir()
                
                logger.info(f"库文件整理完成: {version_dir}")
                return True
                
        except zipfile.BadZipFile as e:
            logger.error(f"无效的ZIP文件: {e}")
            return False
        except Exception as e:
            logger.error(f"解压失败: {e}")
            return False
    
    def download_version(self, version: str) -> bool:
        """
        下载指定版本的SDL2
        时间复杂度: O(1) API调用 + O(s) 下载 + O(f) 解压
        """
        # 规范化版本号
        if version.startswith('release-'):
            version = version[8:]
        
        logger.info(f"开始下载SDL2版本: {version}")
        
        # 查找发布
        releases = self.get_all_releases()
        target_release = None
        
        for release in releases:
            tag = release.get('tag_name', '')
            if tag == f'release-{version}' or tag == version:
                target_release = release
                break
        
        if not target_release:
            logger.error(f"未找到版本: {version}")
            return False
        
        # 查找Windows开发库
        asset = self.find_windows_devel_asset(target_release)
        if not asset:
            logger.error(f"版本 {version} 没有Windows开发库")
            return False
        
        asset_name = asset['name']
        asset_url = asset['browser_download_url']
        asset_size = asset.get('size', 0)
        
        # 下载路径
        zip_path = self.downloads_dir / asset_name
        
        # 检查是否已下载
        if zip_path.exists():
            file_size = zip_path.stat().st_size
            if file_size == asset_size:
                logger.info(f"文件已存在且大小匹配，跳过下载: {asset_name}")
            else:
                logger.warning(f"文件大小不匹配，重新下载: {asset_name}")
                zip_path.unlink()
        
        # 下载
        if not zip_path.exists():
            if not self.download_file(asset_url, zip_path, asset_size):
                return False
        
        # 解压并整理
        return self.extract_and_organize(zip_path, version)
    
    def download_all_versions(self, max_versions: Optional[int] = None) -> Tuple[int, int]:
        """
        下载所有SDL2版本
        返回: (成功数, 总数)
        时间复杂度: O(n * (1 + s + f)) 其中n为版本数
        """
        releases = self.get_all_releases()
        
        if max_versions:
            releases = releases[:max_versions]
        
        total = len(releases)
        success = 0
        
        logger.info(f"开始下载 {total} 个版本")
        
        for i, release in enumerate(releases, 1):
            tag = release.get('tag_name', '')
            version = tag.replace('release-', '')
            
            logger.info(f"[{i}/{total}] 处理版本: {version}")
            
            if self.download_version(version):
                success += 1
            else:
                logger.warning(f"版本 {version} 下载失败，继续下一个")
        
        logger.info(f"下载完成: {success}/{total} 个版本成功")
        return success, total


def main():
    parser = argparse.ArgumentParser(
        description='自动下载SDL2库并整理到Libs文件夹',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  %(prog)s --version 2.28.5          # 下载指定版本
  %(prog)s --all                     # 下载所有版本
  %(prog)s --all --max 10            # 下载最近的10个版本
  %(prog)s --list                    # 列出可用版本
  %(prog)s --version 2.28.5 --proxy http://proxy:8080
        '''
    )
    
    parser.add_argument(
        '--version',
        type=str,
        help='指定要下载的SDL2版本 (例如: 2.28.5)'
    )
    
    parser.add_argument(
        '--all',
        action='store_true',
        help='下载所有可用版本'
    )
    
    parser.add_argument(
        '--max',
        type=int,
        metavar='N',
        help='限制下载版本数量（与--all配合使用）'
    )
    
    parser.add_argument(
        '--list',
        action='store_true',
        help='列出所有可用版本'
    )
    
    parser.add_argument(
        '--output',
        type=str,
        default='.',
        help='输出目录 (默认: 当前目录)'
    )
    
    parser.add_argument(
        '--proxy',
        type=str,
        help='HTTP代理 (例如: http://proxy:8080)'
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='显示详细日志'
    )
    
    args = parser.parse_args()
    
    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # 验证参数
    if not any([args.version, args.all, args.list]):
        parser.print_help()
        sys.exit(1)
    
    try:
        downloader = SDL2Downloader(
            output_dir=Path(args.output),
            proxy=args.proxy
        )
        
        if args.list:
            releases = downloader.get_all_releases()
            print("\n可用SDL2版本:")
            print("-" * 40)
            for release in releases:
                tag = release.get('tag_name', '')
                version = tag.replace('release-', '')
                has_devel = "✓" if downloader.find_windows_devel_asset(release) else "✗"
                print(f"  {version} [Windows库: {has_devel}]")
            print()
            sys.exit(0)
        
        if args.version:
            success = downloader.download_version(args.version)
            sys.exit(0 if success else 1)
        
        if args.all:
            success, total = downloader.download_all_versions(max_versions=args.max)
            sys.exit(0 if success == total else 1)
            
    except KeyboardInterrupt:
        logger.info("用户中断")
        sys.exit(130)
    except Exception as e:
        logger.error(f"执行失败: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
