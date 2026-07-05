#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
授权管理器 (License Manager)
提供 3 天首次试用期限制，到期后需要根据一机一密的机器码索要激活码。
完全使用原生标准库实现数字签名验证，无需任何第三方加解密库依赖，确保打包 100% 成功。
"""

import os
import sys
import time
import hashlib
import base64
import json
import urllib.request

# 只有作者您知道的私钥盐值，用于防止用户伪造激活码。千万不要泄露给任何人！
SECRET_KEY = "HufuMeetingCaptionerKey#2026@Gold"

# 试用期长度：3 天 (单位：秒)
TRIAL_DURATION = 3 * 24 * 3600

# 隐藏的授权文件存储路径（存放在用户家目录下，即使删除项目文件夹也无法清除试用记录）
TRIAL_FILE = os.path.expanduser("~/.captioner_trial.dat")
LICENSE_FILE = os.path.expanduser("~/.captioner_lic.dat")
TIME_FILE = os.path.expanduser("~/.captioner_time.dat")


class LicenseManager(object):
    def __init__(self):
        self.machine_id = self.get_machine_id()

    @staticmethod
    def get_machine_id():
        """
        获取当前电脑的唯一物理机器码 (绑定 CPU 序列号 + 主板 UUID)
        """
        hardware_str = ""
        try:
            if sys.platform == "win32":
                # 读取 CPU 序列号
                import subprocess
                cpu_cmd = "wmic cpu get processorid"
                cpu_info = subprocess.check_output(cpu_cmd, shell=True).decode().strip()
                cpu_id = "".join(cpu_info.split("\n")[1:]).strip()
                
                # 读取主板 UUID
                uuid_cmd = "wmic csproduct get uuid"
                uuid_info = subprocess.check_output(uuid_cmd, shell=True).decode().strip()
                board_uuid = "".join(uuid_info.split("\n")[1:]).strip()
                
                hardware_str = f"{cpu_id}-{board_uuid}"
            else:
                # 兼容非 Windows 系统
                import socket
                hardware_str = socket.gethostname()
        except Exception:
            hardware_str = "DEFAULT_HARDWARE_KEY_FALLBACK"

        # 使用 MD5 将长硬件信息混淆生成漂亮且唯一的 12 位十六进制机器码
        md5 = hashlib.md5(hardware_str.encode("utf-8")).hexdigest().upper()
        return "-".join([md5[i:i+4] for i in range(0, 12, 4)])

    @staticmethod
    def get_network_time():
        """
        向公共网络时间 API 请求获取准确的北京时间时间戳。
        如果获取失败（无网环境），返回 None。
        """
        # 使用苏宁和阿里的公共网络时间接口
        urls = [
            "http://api.m.taobao.com/rest/api3.do?api=mtop.common.getSystemTime",
            "https://f.suining.gov.cn/api/common/time"  # 备用
        ]
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=2.0) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    if "data" in data and "t" in data["data"]:
                        # 淘宝接口返回的是毫秒时间戳
                        return float(data["data"]["t"]) / 1000.0
            except Exception:
                continue
        return None

    def get_safe_time(self):
        """
        获取当前安全的时间戳，并进行系统时间倒退防白嫖校验。
        """
        current_time = self.get_network_time()
        is_net_time = True
        
        if current_time is None:
            # 如果断网，读取本地系统时间
            current_time = time.time()
            is_net_time = False

        # 系统时间篡改校验：读取最后一次正常启动记录的时间
        if os.path.exists(TIME_FILE):
            try:
                with open(TIME_FILE, "r") as f:
                    last_time = float(f.read().strip())
                if current_time < last_time:
                    # 发现系统时间被往回改了，强制惩罚：使用最后记录的运行时间作为当前时间
                    current_time = last_time
            except Exception:
                pass

        # 更新最新运行时间戳
        try:
            with open(TIME_FILE, "w") as f:
                f.write(str(current_time))
        except Exception:
            pass

        return current_time, is_net_time

    def verify_license(self):
        """
        验证授权情况。返回 (bool, info_msg)
        """
        now, is_net = self.get_safe_time()
        
        # === 1. 优先校验永久/限时激活码 ===
        if os.path.exists(LICENSE_FILE):
            try:
                with open(LICENSE_FILE, "r") as f:
                    lic_key = f.read().strip()
                is_ok, expire_date_str = self.check_license_key(self.machine_id, lic_key)
                if is_ok:
                    if expire_date_str == "PERMANENT":
                        return True, "已永久激活授权"
                    else:
                        # 校验激活码的截止日期
                        expire_ts = time.mktime(time.strptime(expire_date_str, "%Y-%m-%d"))
                        if now <= expire_ts:
                            days_left = int((expire_ts - now) / 86400) + 1
                            return True, f"激活码授权中 (剩余 {days_left} 天)"
                        else:
                            return False, "激活码授权已过期，请索要新激活码"
            except Exception as e:
                pass

        # === 2. 激活码不存在或无效，进入 3 天免费试用期校验 ===
        trial_start = None
        if os.path.exists(TRIAL_FILE):
            try:
                with open(TRIAL_FILE, "r") as f:
                    # 解密出首次启动时间
                    encrypted = f.read().strip()
                    decrypted = base64.b64decode(encrypted).decode("utf-8")
                    trial_start = float(decrypted)
            except Exception:
                pass

        if trial_start is None:
            # 第一次打开软件，写入当前安全时间作为首次启动起点
            trial_start = now
            try:
                encrypted = base64.b64encode(str(trial_start).encode("utf-8")).decode("utf-8")
                # 确保目录存在
                os.makedirs(os.path.dirname(TRIAL_FILE), exist_ok=True)
                with open(TRIAL_FILE, "w") as f:
                    f.write(encrypted)
            except Exception:
                pass

        # 计算试用期截止时间
        trial_end = trial_start + TRIAL_DURATION
        if now <= trial_end:
            time_left_sec = trial_end - now
            hours_left = int(time_left_sec / 3600)
            if hours_left >= 24:
                return True, f"处于3天免费试用期内 (还剩 {int(hours_left/24)} 天)"
            else:
                return True, f"处于3天免费试用期内 (还剩 {hours_left} 小时)"
        else:
            return False, "3天免费试用期已结束，请联系作者输入激活码"

    @classmethod
    def generate_license_key(cls, machine_id, expire_date_str="PERMANENT"):
        """
        【作者专用生成方法】
        输入买家的机器码和截止日期(格式如 "2026-07-15" 或 "PERMANENT")，生成专属防伪激活码。
        激活码格式：机器码-截止日期-数字签名签名哈希
        """
        # 清理和校验机器码格式
        machine_id = machine_id.strip().upper()
        raw_data = f"{machine_id}|{expire_date_str}"
        
        # 使用哈希混淆私钥生成不可逆防伪数字签名
        sign_src = f"{raw_data}|{SECRET_KEY}"
        signature = hashlib.sha256(sign_src.encode("utf-8")).hexdigest()[:12].upper()
        
        # 拼接最终激活码
        lic_key = f"{machine_id}-{expire_date_str}-{signature}"
        return lic_key

    @classmethod
    def check_license_key(cls, machine_id, lic_key):
        """
        验证激活码合法性。
        返回 (bool, expire_date_str)
        """
        try:
            parts = lic_key.strip().split("-")
            if len(parts) < 5:
                return False, "激活码格式错误"
            
            # 前三段拼接出机器码
            input_machine_id = "-".join(parts[0:3]).upper()
            expire_date_str = parts[3].upper()
            signature = parts[4].upper()
            
            # 比对机器码是否属于本机
            if input_machine_id != machine_id.upper():
                return False, "机器码不匹配"
            
            # 重新计算本地数字签名，比对是否被篡改
            raw_data = f"{input_machine_id}|{expire_date_str}"
            sign_src = f"{raw_data}|{SECRET_KEY}"
            expected_signature = hashlib.sha256(sign_src.encode("utf-8")).hexdigest()[:12].upper()
            
            if signature == expected_signature:
                return True, expire_date_str
            else:
                return False, "激活码签名校验失败"
        except Exception:
            return False, "非法激活码"

    def save_license(self, lic_key):
        """
        将用户输入的激活码保存到本地
        """
        is_ok, msg = self.check_license_key(self.machine_id, lic_key)
        if is_ok:
            try:
                os.makedirs(os.path.dirname(LICENSE_FILE), exist_ok=True)
                with open(LICENSE_FILE, "w") as f:
                    f.write(lic_key.strip())
                return True, "激活成功"
            except Exception as e:
                return False, f"写入激活文件失败: {e}"
        else:
            return False, msg


# ============================================================================
# 作者命令行快捷生成入口
# ============================================================================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="会议字幕助手激活码生成工具")
    parser.add_argument("--machine", type=str, help="买家的机器码 (例如 8A2F-9D4B-CE1A)")
    parser.add_argument("--days", type=int, help="授权使用天数 (例如 30 代表一个月，不填代表永久授权)")
    args = parser.parse_args()

    if args.machine:
        expire_str = "PERMANENT"
        if args.days:
            future_ts = time.time() + args.days * 86400
            expire_str = time.strftime("%Y-%m-%d", time.localtime(future_ts))
        
        key = LicenseManager.generate_license_key(args.machine, expire_str)
        print("\n" + "="*50)
        print(f"Target Machine ID: {args.machine}")
        print(f"Expire Date: {expire_str if args.days else 'PERMANENT'}")
        print("-"*50)
        print(f"License Key:\n{key}")
        print("="*50 + "\n")
    else:
        mgr = LicenseManager()
        print(f"\nLocal Machine ID: {mgr.machine_id}")
        is_valid, info = mgr.verify_license()
        print(f"Auth Status: {info}\n")
