# trading_time.py
import json
import re
import csv
import os
from pathlib import Path

import requests


class TradingTime:
    """交易时间管理类 - 仅用于获取和保存交易时间数据"""
    
    # API端点
    API_URL = "http://dict.openctp.cn/times"
    
    def __init__(self, config_path=None):
        """
        初始化交易时间管理器
        
        Args:
            config_path: 策略配置文件路径，默认为 .vntrader/cta_strategy_setting.json
        """
        if config_path is None:
            # 尝试多个可能的路径
            possible_paths = [
                ".vntrader/cta_strategy_setting.json",
                os.path.expanduser("~/.vntrader/cta_strategy_setting.json"),
            ]
            
            self.config_path = None
            self.vntrader_dir = None  # 保存.vntrader目录路径
            
            for path in possible_paths:
                if Path(path).exists():
                    self.config_path = Path(path)
                    self.vntrader_dir = self.config_path.parent
                    break
            
            if self.config_path is None:
                self.config_path = Path(".vntrader/cta_strategy_setting.json")
                self.vntrader_dir = Path(".vntrader")
        else:
            self.config_path = Path(config_path)
            self.vntrader_dir = self.config_path.parent
        
        self.trading_data = {}
        self._load_and_update()
    
    def _extract_symbols(self) -> list:
        """从配置文件提取交易标的"""
        print(f"🔍 查找配置文件: {self.config_path}")
        
        if not self.config_path.exists():
            print(f"❌ 配置文件不存在: {self.config_path}")
            print(f"   当前工作目录: {os.getcwd()}")
            return []
        
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            print(f"✅ 配置文件加载成功，包含 {len(config)} 个策略")
        except Exception as e:
            print(f"❌ 读取配置文件失败: {e}")
            return []
        
        # 提取品种代码
        symbols = set()
        for strategy_name, strategy_config in config.items():
            vt_symbol = strategy_config.get('vt_symbol', '')
            if vt_symbol:
                # 提取品种代码（去掉数字后缀）
                symbol_code = vt_symbol.split('.')[0]
                symbol_code = re.sub(r'\d+$', '', symbol_code)
                symbols.add(symbol_code.lower())
                print(f"   📌 {strategy_name}: {vt_symbol} -> {symbol_code}")
        
        if not symbols:
            print("⚠️ 未从配置文件中提取到任何品种代码")
            print("   请检查配置文件中是否包含 'vt_symbol' 字段")
            return []
        
        print(f"✅ 提取到 {len(symbols)} 个品种: {', '.join(sorted(symbols))}")
        return list(symbols)
    
    def _fetch_trading_times(self, symbols: list) -> dict:
        """从API获取交易时间"""
        if not symbols:
            return {}
        
        try:
            products = ','.join(symbols)
            url = f"{self.API_URL}?types=futures&products={products}"
            
            print(f"🌐 请求API: {url}")
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            print(f"📊 API响应码: {data.get('rsp_code')}")
            
            if data.get('rsp_code') != 0:
                print(f"❌ API错误: {data.get('rsp_message')}")
                return {}
            
            raw_data = data.get('data', [])
            print(f"✅ 获取到 {len(raw_data)} 条交易时间记录")
            
            if raw_data:
                # 打印前3条记录作为示例
                print("📋 数据示例:")
                for i, record in enumerate(raw_data[:3]):
                    print(f"   {i+1}. {record.get('ProductID')} {record.get('TimeBegin')}-{record.get('TimeEnd')}")
            
            return self._parse_trading_data(raw_data)
            
        except requests.RequestException as e:
            print(f"❌ API请求失败: {e}")
            return {}
    
    def _parse_trading_data(self, raw_data: list) -> dict:
        """解析并整理交易时间数据"""
        # 按品种分组
        groups = {}
        for record in raw_data:
            product_id = record.get('ProductID')
            if product_id:
                groups.setdefault(product_id, []).append(record)
        
        result = {}
        for product_id, segments in groups.items():
            # 按段号排序
            segments.sort(key=lambda x: int(x.get('SegmentNo', 0)))
            
            # 分离白盘和夜盘
            day_segments = []
            night_segments = []
            
            for seg in segments:
                time_begin = seg.get('TimeBegin', '')
                hour = int(time_begin.split(':')[0]) if time_begin else 0
                
                if 18 <= hour or hour < 6:
                    night_segments.append(seg)
                else:
                    day_segments.append(seg)
            
            result[product_id] = {
                'ProductID': product_id,
                'ExchangeID': segments[0].get('ExchangeID', ''),
                'has_night': bool(night_segments),
                'day_segments': day_segments,
                'night_segments': night_segments
            }
        
        return result
    
    def _load_and_update(self):
        """加载并更新交易时间"""
        symbols = self._extract_symbols()
        
        if not symbols:
            print("⚠️ 未找到交易标的，请检查配置文件")
            return
        
        self.trading_data = self._fetch_trading_times(symbols)
        
        if self.trading_data:
            print(f"✅ 成功获取 {len(self.trading_data)} 个品种的交易时间")
        else:
            print("❌ 未能获取交易时间数据")
    
    def save_to_csv(self, output_path=None) -> bool:
        """
        保存交易时间到CSV文件（每个品种一行）
        
        Args:
            output_path: 输出文件路径，默认为 .vntrader/trading_times.csv
            
        Returns:
            bool: 是否保存成功
        """
        if output_path is None:
            # 使用找到的.vntrader目录
            if self.vntrader_dir:
                output_path = self.vntrader_dir / "trading_times.csv"
            else:
                output_path = Path(".vntrader/trading_times.csv")
        
        output_path = Path(output_path)
        
        if not self.trading_data:
            print("⚠️ 没有交易时间数据可保存")
            return False
        
        try:
            output_dir = output_path.parent
            output_dir.mkdir(parents=True, exist_ok=True)
            
            with open(output_path, 'w', newline='', encoding='utf-8-sig') as f:
                fieldnames = [
                    'ProductID', 
                    'ExchangeID',
                    'has_night',
                    'day_trading_hours',
                    'night_trading_hours',
                    'day_break_hours',
                    'night_break_hours'
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                
                for product_id, info in sorted(self.trading_data.items()):
                    # 格式化白盘时段（用 | 分隔不同段）
                    day_hours = ' | '.join([f"{s.get('TimeBegin', '')[:5]}-{s.get('TimeEnd', '')[:5]}" 
                                           for s in info['day_segments']])
                    
                    # 格式化夜盘时段
                    night_hours = ' | '.join([f"{s.get('TimeBegin', '')[:5]}-{s.get('TimeEnd', '')[:5]}" 
                                             for s in info['night_segments']]) if info['has_night'] else ''
                    
                    # 识别休息时段（白盘）
                    day_breaks = []
                    if len(info['day_segments']) > 1:
                        for i in range(len(info['day_segments']) - 1):
                            end_time = info['day_segments'][i].get('TimeEnd', '')
                            start_time = info['day_segments'][i + 1].get('TimeBegin', '')
                            if end_time and start_time:
                                day_breaks.append(f"{end_time[:5]}-{start_time[:5]}")
                    
                    # 识别休息时段（夜盘）
                    night_breaks = []
                    if len(info['night_segments']) > 1:
                        for i in range(len(info['night_segments']) - 1):
                            end_time = info['night_segments'][i].get('TimeEnd', '')
                            start_time = info['night_segments'][i + 1].get('TimeBegin', '')
                            if end_time and start_time:
                                night_breaks.append(f"{end_time[:5]}-{start_time[:5]}")
                    
                    writer.writerow({
                        'ProductID': info['ProductID'],
                        'ExchangeID': info['ExchangeID'],
                        'has_night': '是' if info['has_night'] else '否',
                        'day_trading_hours': day_hours,
                        'night_trading_hours': night_hours,
                        'day_break_hours': ' | '.join(day_breaks),
                        'night_break_hours': ' | '.join(night_breaks)
                    })
            
            print(f"✅ 交易时间已保存到: {output_path.absolute()}")
            print(f"   共 {len(self.trading_data)} 个品种")
            
            # 打印数据预览
            self._print_preview()
            return True
            
        except Exception as e:
            print(f"❌ 保存CSV失败: {e}")
            return False
    
    def _print_preview(self):
        """打印数据预览"""
        print("\n📊 数据预览:")
        for pid, info in list(self.trading_data.items())[:5]:
            print(f"  📌 {pid} ({info['ExchangeID']})")
            
            # 显示白盘
            if info['day_segments']:
                day_str = ' | '.join([f"{s.get('TimeBegin', '')[:5]}-{s.get('TimeEnd', '')[:5]}" 
                                     for s in info['day_segments']])
                print(f"     白盘: {day_str}")
                
                # 显示休息时间
                if len(info['day_segments']) > 1:
                    breaks = []
                    for i in range(len(info['day_segments']) - 1):
                        end = info['day_segments'][i].get('TimeEnd', '')[:5]
                        start = info['day_segments'][i + 1].get('TimeBegin', '')[:5]
                        if end and start:
                            breaks.append(f"{end}-{start}")
                    if breaks:
                        print(f"     休息: {' | '.join(breaks)}")
            
            # 显示夜盘
            if info['night_segments']:
                night_str = ' | '.join([f"{s.get('TimeBegin', '')[:5]}-{s.get('TimeEnd', '')[:5]}" 
                                       for s in info['night_segments']])
                print(f"     夜盘: {night_str}")
                
                # 显示休息时间
                if len(info['night_segments']) > 1:
                    breaks = []
                    for i in range(len(info['night_segments']) - 1):
                        end = info['night_segments'][i].get('TimeEnd', '')[:5]
                        start = info['night_segments'][i + 1].get('TimeBegin', '')[:5]
                        if end and start:
                            breaks.append(f"{end}-{start}")
                    if breaks:
                        print(f"     休息: {' | '.join(breaks)}")




def run_and_save(config_path=None) -> bool:
    """可导入函数：加载交易时间并保存到CSV（适合被 run_cta.py 启动时调用）"""
    try:
        tt = TradingTime(config_path)
        if tt.trading_data:
            return tt.save_to_csv()
        else:
            print("⚠️ 未获取到交易时间数据，跳过保存")
            # 如果 CSV 已存在，不阻塞启动
            return False
    except Exception as e:
        print(f"⚠️ 更新交易时间失败（不影响启动）: {e}")
        return False


def main():
    """测试函数：更新交易时间文件"""
    print("=" * 60)
    print("  📅 更新交易时间数据")
    print("=" * 60)
    
    print(f"📁 当前工作目录: {os.getcwd()}")
    
    # 创建交易时间管理器
    trading_time = TradingTime()
    
    # 检查是否获取到数据
    if trading_time.trading_data:
        # 保存到CSV
        success = trading_time.save_to_csv()
        if success:
            print("\n✅ 交易时间更新完成！")
        else:
            print("\n❌ 保存交易时间失败")
    else:
        print("\n⚠️ 未获取到任何交易时间数据，未保存文件")
        print("   请检查:")
        print("   1. 配置文件是否存在: .vntrader/cta_strategy_setting.json")
        print("   2. 网络连接是否正常")
        print("   3. API服务是否可用")

        print("\n💡 提示: 可以尝试指定配置文件路径:")
        print("   trading_time = TradingTime('.vntrader/cta_strategy_setting.json')")
    
    print("=" * 60)


if __name__ == "__main__":
    main()