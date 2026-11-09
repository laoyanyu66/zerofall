"""
生成 ZeroFall 图标
"""
from PIL import Image, ImageDraw, ImageFont
import os

def create_zerofall_icon():
    """生成 ZeroFall 图标"""
    # 创建 256x256 画布
    size = 256
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # 绘制盾牌背景（渐变蓝色）
    # 盾牌路径
    shield_points = [
        (size//2, 10),           # 顶部中心
        (size-20, 50),           # 右上
        (size-20, size//2),      # 右中
        (size//2, size-10),      # 底部中心
        (20, size//2),           # 左中
        (20, 50),                # 左上
        (size//2, 10),           # 回到顶部
    ]
    
    # 绘制盾牌外框
    draw.polygon(shield_points, fill=(30, 144, 255, 255), outline=(0, 100, 200, 255))
    
    # 绘制内层盾牌（浅色）
    inner_shield = [
        (size//2, 30),
        (size-40, 60),
        (size-40, size//2-10),
        (size//2, size-30),
        (40, size//2-10),
        (40, 60),
        (size//2, 30),
    ]
    draw.polygon(inner_shield, fill=(70, 180, 255, 255))
    
    # 绘制 "ZF" 文字
    try:
        # 尝试使用系统字体
        font = ImageFont.truetype("arial.ttf", 80)
    except:
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 80)
        except:
            font = ImageFont.load_default()
    
    text = "ZF"
    # 计算文字位置（居中）
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    text_x = (size - text_width) // 2
    text_y = (size - text_height) // 2 - 10
    
    # 绘制文字阴影
    draw.text((text_x+2, text_y+2), text, fill=(0, 50, 100, 200), font=font)
    # 绘制文字
    draw.text((text_x, text_y), text, fill=(255, 255, 255, 255), font=font)
    
    # 保存为多个尺寸的 ico 文件
    img.save('icon.png', 'PNG')
    
    # 生成多尺寸 ICO
    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save('ZeroFall.ico', format='ICO', sizes=ico_sizes)
    
    print("图标生成完成:")
    print("  - icon.png")
    print("  - ZeroFall.ico")

if __name__ == "__main__":
    create_zerofall_icon()
