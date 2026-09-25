#!/usr/bin/env python3
"""
胶囊的画法 —— 自己出图，不借 Chrome。

外形是关键：它是一块**离开屏幕上沿、四角全圆的悬浮方块**（470×140，圆角 34），
不是贴着边缘的通栏横条 —— 横条是状态栏的形状，怎么调内容都不会像灵动岛。

为什么不用 Chrome：它的 --app 窗口自己画一圈浅色框架和标题栏，那圈东西走它
自己的合成层，SetWindowRgn 裁不到，所以边上总挂一道白边、底角是方的。现在是
PIL 出图 + UpdateLayeredWindow 逐像素透明，圆角真正抗锯齿。

性能上两条讲究，不注意动画就会卡：
  · 内容层缓存，只有数据真变了才重画（背景渐变是逐行画的，每帧重画必掉帧）
  · 直接产出预乘 alpha 的 BGRA —— 遮罩乘一遍 RGB 得到的正好是预乘结果
"""
import colorsys
import os

import paths
import re

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ART_DIR = paths.res("art")          # 自带图；用户自己的图放 DATA 那份
ART_USER = paths.data("art")

# ── 皮肤 ────────────────────────────────────────────────────────────
SKINS = {
    "obsidian": dict(name="曜石", bg1=(32, 32, 35), bg2=(12, 12, 14),
                     ink=(245, 245, 247), ink2=(152, 152, 157), ink3=(98, 98, 104),
                     track=(255, 255, 255, 32), ok=(48, 209, 88), warn=(255, 159, 10),
                     bad=(255, 69, 58), info=(10, 132, 255),
                     pet=(217, 119, 87), pet2=(242, 184, 160), art="octopus"),
    "frost":    dict(name="霜", bg1=(252, 252, 254), bg2=(228, 228, 236),
                     ink=(29, 29, 31), ink2=(108, 108, 114), ink3=(158, 158, 166),
                     track=(0, 0, 0, 30), ok=(52, 199, 89), warn=(255, 149, 0),
                     bad=(255, 59, 48), info=(0, 122, 255),
                     pet=(217, 119, 87), pet2=(180, 85, 58), art="octopus"),
    "eva01":    dict(name="初号机", bg1=(40, 28, 60), bg2=(14, 10, 25),
                     ink=(240, 235, 255), ink2=(169, 155, 201), ink3=(112, 100, 140),
                     track=(160, 120, 255, 46), ok=(123, 224, 74), warn=(255, 159, 10),
                     bad=(255, 69, 58), info=(157, 123, 255),
                     pet=(123, 224, 74), pet2=(255, 106, 31), art="eva"),
    "pika":     dict(name="皮卡丘", bg1=(48, 37, 23), bg2=(17, 13, 7),
                     ink=(255, 246, 224), ink2=(199, 177, 131), ink3=(132, 115, 82),
                     track=(255, 204, 51, 46), ok=(255, 204, 51), warn=(255, 159, 10),
                     bad=(228, 87, 46), info=(255, 224, 138),
                     pet=(255, 204, 51), pet2=(228, 87, 46), art="pika"),
    "matcha":   dict(name="苔", bg1=(25, 40, 30), bg2=(8, 14, 10),
                     ink=(234, 246, 238), ink2=(147, 175, 158), ink3=(96, 120, 106),
                     track=(120, 220, 160, 42), ok=(74, 222, 128), warn=(250, 204, 21),
                     bad=(248, 113, 113), info=(103, 232, 249),
                     pet=(74, 222, 128), pet2=(250, 204, 21), art="leaf"),
    "abyss":    dict(name="深海", bg1=(18, 34, 52), bg2=(5, 12, 21),
                     ink=(226, 240, 255), ink2=(139, 168, 196), ink3=(90, 112, 134),
                     track=(120, 190, 255, 38), ok=(94, 234, 212), warn=(255, 183, 77),
                     bad=(255, 110, 110), info=(96, 165, 250),
                     pet=(125, 190, 225), pet2=(226, 240, 255), art="shark"),
    "sakura":   dict(name="樱", bg1=(56, 32, 44), bg2=(21, 11, 17),
                     ink=(255, 235, 243), ink2=(212, 165, 185), ink3=(144, 108, 124),
                     track=(255, 170, 200, 42), ok=(255, 150, 190), warn=(255, 190, 120),
                     bad=(255, 105, 120), info=(200, 170, 255),
                     pet=(255, 160, 195), pet2=(255, 235, 243), art="cat"),
    "lava":     dict(name="熔岩", bg1=(52, 22, 16), bg2=(17, 7, 5),
                     ink=(255, 236, 224), ink2=(214, 158, 132), ink3=(138, 98, 82),
                     track=(255, 120, 60, 42), ok=(255, 176, 59), warn=(255, 120, 40),
                     bad=(255, 70, 50), info=(255, 205, 120),
                     pet=(255, 140, 60), pet2=(255, 226, 168), art="charizard"),
    "nord":     dict(name="极夜", bg1=(50, 56, 68), bg2=(25, 29, 37),
                     ink=(236, 239, 244), ink2=(168, 178, 194), ink3=(114, 124, 140),
                     track=(216, 222, 233, 34), ok=(163, 190, 140), warn=(235, 203, 139),
                     bad=(191, 97, 106), info=(136, 192, 208),
                     pet=(136, 192, 208), pet2=(236, 239, 244), art="penguin"),
    "cyber":    dict(name="赛博", bg1=(26, 16, 45), bg2=(9, 6, 20),
                     ink=(233, 240, 255), ink2=(150, 160, 210), ink3=(100, 104, 154),
                     track=(0, 240, 255, 36), ok=(0, 240, 200), warn=(255, 210, 60),
                     bad=(255, 70, 140), info=(0, 200, 255),
                     pet=(0, 240, 255), pet2=(255, 70, 140), art="bolt"),
}
SKIN_ORDER = ["obsidian", "nord", "abyss", "matcha", "sakura", "lava",
              "pika", "eva01", "cyber", "frost"]
SKIN_OF = [("皮卡丘", "pika"), ("pika", "pika"),
           ("初号机", "eva01"), ("零号机", "eva01"), ("eva", "eva01"),
           ("我的世界", "matcha"), ("苔", "matcha"), ("森", "matcha"), ("蔬菜", "matcha"),
           ("鲨鱼", "abyss"), ("水", "abyss"), ("海", "abyss"), ("初音", "abyss"),
           ("樱", "sakura"), ("心跳", "sakura"), ("熊猫", "nord"),
           ("岩浆", "lava"), ("火", "lava"), ("柴犬", "lava"), ("水果", "lava"),
           ("霜", "frost"), ("雪", "frost"), ("极光", "cyber"), ("赛博", "cyber")]


def skin_for(theme, manual=None):
    if manual and manual in SKINS:
        return SKINS[manual]
    for key, name in SKIN_OF:
        if theme and key.lower() in str(theme).lower():
            return SKINS[name]
    return SKINS["obsidian"]


# ── 字体 ────────────────────────────────────────────────────────────
_FONT_CACHE = {}
UI_FONT = "C:/Windows/Fonts/msyh.ttc"
UI_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
MONO_FONT = "C:/Windows/Fonts/consola.ttf"


def font(px, bold=False, mono=False):
    key = (px, bold, mono)
    f = _FONT_CACHE.get(key)
    if f is None:
        path = MONO_FONT if mono else (UI_BOLD if bold else UI_FONT)
        try:
            f = ImageFont.truetype(path, px, index=0)
        except Exception:
            f = ImageFont.load_default()
        _FONT_CACHE[key] = f
    return f


def flat(x):
    """PIL 的 textlength 遇到换行会直接抛错，会话消息里常有换行。"""
    return re.sub(r"\s+", " ", str(x if x is not None else "")).strip()


def numfont(px, text):
    """数字用等宽的对得齐，可 consola 没有中文（"3天5时"会变方块）。"""
    return font(px, mono=all(ord(c) < 128 for c in str(text)))


ST_TXT = {"idle": "空闲", "busy": "执行中", "wait": "等你", "done": "完成", "error": "出错"}
PRI = {"wait": 4, "error": 3, "busy": 2, "done": 1, "idle": 0}

# ── 电子宠物：#=身体 w=白 p=瞳孔 y=点缀色 ────────────────────────────
PETS = {
    "octopus": dict(name="小章鱼", eyes=(5, 6), art=[
        "......#####......", "....#########....", "...###########...",
        "..#############..", "..#############..", "..###ww###ww###..",
        "..###wp###wp###..", "..#############..", "...###########...",
        "...###########...",
    ], legs=[["...##.##.##.##...", "....#..#..#..#...", "....#.....#......"],
             ["...##.##.##.##...", "...#..#..#..#....", "......#.....#...."]]),
    "shark": dict(name="鲨鱼", eyes=(6, 7), art=[
        "........#........", ".......###.......", "......#####......",
        "....#########....", "..#############..", ".###############.",
        ".###ww#####ww###.", ".###pp#####pp###.", ".################",
        "..#############..",
    ], legs=[["..#wwwwwwwwwww#..", "...###########...", "....#########...."],
             ["..#w#w#w#w#w#w#..", "...###########...", ".....#######....."]]),
    "cat": dict(name="猫", eyes=(5, 6), art=[
        "..##.........##..", "..###.......###..", "..#############..",
        ".###############.", ".###############.", ".###ww#####ww###.",
        ".###pp#####pp###.", ".#######y#######.", ".################",
        "..#############..",
    ], legs=[["...###########...", "....##.....##....", "....##.....##...."],
             ["...###########...", "...##.......##...", "...##.......##..."]]),
    "penguin": dict(name="企鹅", eyes=(5, 6), art=[
        ".....#######.....", "...###########...", "..#############..",
        ".###############.", ".###############.", ".###ww#####ww###.",
        ".###pp#####pp###.", ".######yyy######.", ".################",
        "..###wwwwwww###..",
    ], legs=[["..##wwwwwwwww##..", "...###########...", "...yy.......yy..."],
             ["..##wwwwwwwww##..", "...###########...", "..yy.........yy.."]]),
}
# ── 用几何形状画角色，再降采样成像素块 ─────────────────────────────
# 手摆像素点很难把比例控住（头会画方、耳朵会变犄角）。改成先用椭圆和多边形
# 把形状搭出来，再缩到 ~34 列的网格上取阈值 —— 形状可控，像素味也还在。
# 眼睛和腮红是挖出来的洞，低透明度下这些洞才是"认得出是谁"的关键。
def _pika_mask(w, h):
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    d.ellipse([w * .17, h * .34, w * .73, h * .95], fill=255)              # 头
    d.polygon([(w * .28, h * .46), (w * .40, h * .40),
               (w * .16, h * .03), (w * .07, h * .09)], fill=255)          # 左耳
    d.polygon([(w * .52, h * .40), (w * .64, h * .46),
               (w * .84, h * .05), (w * .75, h * .01)], fill=255)          # 右耳
    d.polygon([(w * .70, h * .74), (w * .84, h * .54), (w * .77, h * .49),
               (w * .96, h * .27), (w * 1.0, h * .40), (w * .88, h * .53),
               (w * .94, h * .59), (w * .76, h * .86)], fill=255)          # 之字尾
    for box in ([w * .30, h * .54, w * .38, h * .63],                      # 眼
                [w * .52, h * .54, w * .60, h * .63],
                [w * .21, h * .68, w * .31, h * .78],                      # 腮红
                [w * .59, h * .68, w * .69, h * .78]):
        d.ellipse(box, fill=0)
    return m


def _charizard_mask(w, h):
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    d.polygon([(w * .30, h * .40), (w * .52, h * .30), (w * .60, h * .46),
               (w * .46, h * .62), (w * .28, h * .58)], fill=255)          # 身
    d.ellipse([w * .12, h * .26, w * .34, h * .48], fill=255)              # 头
    d.polygon([(w * .13, h * .34), (w * .02, h * .40), (w * .14, h * .45)], fill=255)  # 吻
    d.polygon([(w * .26, h * .28), (w * .34, h * .06), (w * .38, h * .30)], fill=255)  # 角
    d.polygon([(w * .34, h * .34), (w * .20, h * .02), (w * .62, h * .10),
               (w * .58, h * .38)], fill=255)                              # 后翼
    d.polygon([(w * .40, h * .44), (w * .70, h * .16), (w * .98, h * .30),
               (w * .66, h * .54)], fill=255)                              # 前翼
    d.polygon([(w * .50, h * .58), (w * .74, h * .72), (w * .70, h * .80),
               (w * .46, h * .66)], fill=255)                              # 尾
    d.polygon([(w * .72, h * .70), (w * .90, h * .62), (w * .82, h * .80),
               (w * .94, h * .86), (w * .70, h * .88)], fill=255)          # 尾焰
    d.polygon([(w * .26, h * .48), (w * .34, h * .70), (w * .44, h * .60)], fill=255)  # 腿
    # 翼和身之间切一道缝，否则整只糊成一团黑
    d.polygon([(w * .38, h * .30), (w * .60, h * .16), (w * .64, h * .22),
               (w * .42, h * .38)], fill=0)
    d.polygon([(w * .44, h * .46), (w * .70, h * .28), (w * .74, h * .34),
               (w * .48, h * .54)], fill=0)
    d.ellipse([w * .17, h * .33, w * .23, h * .39], fill=0)                # 眼
    return m


def _pixelate(fn, cols=34):
    """几何形状 -> 像素网格。先按 8 倍画再缩，形状不会碎。"""
    w, h = cols * 8, int(cols * 8 * 0.62)
    m = fn(w, h)
    rows = int(cols * 0.62)
    small = m.resize((cols, rows), Image.BILINEAR)
    px = small.load()
    return ["".join("#" if px[x, y] > 110 else "." for x in range(cols))
            for y in range(rows)]


# 背景水印用的角色。压在文字后面、透明度很低，所以只要轮廓认得出就行；
# 'p'（瞳孔）画成镂空，眼睛才不会糊成一块。
ART = {
    "bolt": [
        "..........####",
        ".........####.",
        "........####..",
        ".......####...",
        "......########",
        ".....#########",
        "....####.####.",
        "........####..",
        ".......####...",
        "......####....",
        ".....####.....",
        "....###.......",
        "...##.........",
    ],
    "flame": [
        ".......##......",
        "......####.....",
        ".....######....",
        "....###..###...",
        "...###....###..",
        "...##......###.",
        "..###.......##.",
        "..###...##..##.",
        "..###..####.##.",
        "...###.####.##.",
        "....##########.",
        ".....########..",
        "......######...",
    ],
    "leaf": [
        "............###",
        "..........#####",
        "........#######",
        "......#########",
        ".....#####..###",
        "....#####...###",
        "...#####....###",
        "..#####...#####",
        "..####..#######",
        "..###.#########",
        "..##.#####.....",
        "..#.###........",
        "..####.........",
    ],
    "eva": [
        "....#########....",
        "...###########...",
        "..#############..",
        "..###pp###pp###..",
        "..##ppppp#ppp###.",
        "..#############..",
        "...###########...",
        "....#########....",
        ".....#######.....",
        "......##.##......",
        ".....###.###.....",
        "....####.####....",
        "...#####.#####...",
    ],
}
ART["pika"] = _pixelate(_pika_mask)
ART["charizard"] = _pixelate(_charizard_mask)
for _k in ("octopus", "shark", "cat", "penguin"):
    ART[_k] = None                      # 用宠物图样，等下面 PETS 定义好再填

PET_ORDER = ["octopus", "shark", "cat", "penguin"]
for _k in PET_ORDER:                    # 宠物图样直接拿来当水印
    ART[_k] = PETS[_k]["art"] + PETS[_k]["legs"][0]
EYE_SYM = {"blink": "--", "happy": "^^", "cross": "xx"}


def _eyes(rows, eyes, kind):
    a, b = eyes
    out = list(rows)
    out[a] = out[a].replace("ww", EYE_SYM[kind])
    out[b] = out[b].replace("wp", EYE_SYM[kind]).replace("pp", EYE_SYM[kind])
    return out


ANIMS = {
    "island": "灵动岛 · 从一颗药丸长开",
    "drop":   "下坠 · 从上方落下来",
    "unfold": "展开 · 上下铺开",
    "fade":   "淡入 · 轻轻浮现",
}
ANIM_ORDER = ["island", "drop", "unfold", "fade"]


def hms(sec):
    sec = max(0, int(sec))
    if sec >= 86400:
        d, h = sec // 86400, sec % 86400 // 3600
        return "%d天%s" % (d, (" %d时" % h) if h else "")
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def win_label(k):
    if k == "five_hour":
        return "5 小时"
    if k == "seven_day":
        return "本周"
    if k == "extra_usage":
        return "额外"
    m = k.replace("seven_day_", "")
    return m[:1].upper() + m[1:]


def win_order(k):
    """排序：本周（主角）> 按模型拆分的周额度 > 5 小时。
    模型那格是真正会先卡住人的，放在 5 小时上面。"""
    if k == "seven_day":
        return 0
    if k == "extra_usage":
        return 9
    if k == "five_hour":
        return 5
    return 2                              # seven_day_<模型>


def _tag_color(name):
    """名字决定色相：coder 永远一个颜色、writer 永远另一个，扫一眼认得出。"""
    h = 0
    for c in str(name):
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, 0.72, 0.80)
    return int(r * 255), int(g * 255), int(b * 255)


class Renderer:
    """画布 = 窗口（含上方留白和抗锯齿余量）；胶囊本体是画布里那块圆角方块。

    content() 只画胶囊本体那么大的一张 RGB 图（可缓存）；
    frame() 按动画进度把它贴进画布并裁成当前形状，直接产出预乘 BGRA。
    """

    def __init__(self, cw, ch, scale, margin, gap):
        self.cw, self.ch = cw, ch          # 胶囊本体（物理像素）
        self.s, self.M, self.T = scale, margin, gap
        self.W = cw + margin * 2
        self.H = ch + gap + margin
        self._bd = {}

    def px(self, v):
        return int(round(v * self.s))

    def backdrop(self, sk):
        key = (id(sk), self.cw, self.ch)
        img = self._bd.get(key)
        if img is None:
            img = Image.new("RGB", (self.cw, self.ch), sk["bg1"])
            d = ImageDraw.Draw(img)
            c1, c2 = sk["bg1"], sk["bg2"]
            for y in range(self.ch):
                t = min(1.0, y / max(1.0, self.ch * 0.9))
                d.line([(0, y), (self.cw, y)],
                       fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
            img = self.watermark(img, sk)
            self._bd = {key: img}
        return img

    def custom_art(self, sk):
        """art/ 目录里放了同名图片就用它 —— 你想要什么角色放什么，
        比我手画的准。支持 png/jpg/webp/gif；带透明通道的用透明通道，
        没有的就按亮度抠（浅色背景的图会自动去底）。"""
        names = [sk.get("art"), sk.get("name")]
        for n in names:
            if not n:
                continue
            for ext in (".png", ".webp", ".gif", ".jpg", ".jpeg"):
                # 用户自己放的图优先 —— 打包成 exe 后 ART_DIR 是临时解压目录，
                # 用户只能往 exe 旁边的 art/ 放东西。
                fp = next((q for q in (os.path.join(ART_USER, str(n) + ext),
                                       os.path.join(ART_DIR, str(n) + ext))
                           if os.path.exists(q)), None)
                if fp:
                    try:
                        return Image.open(fp)
                    except Exception:
                        pass
        return None

    def watermark(self, img, sk):
        """把皮肤对应的角色压在背景里 —— 大、偏右、出血到边缘、透明度很低，
        像是文字后面透出来的一层。烘进缓存的背景层，所以每帧零开销。"""
        pic = self.custom_art(sk)
        if pic is not None:
            return self._wm_image(img, sk, pic)
        rows = ART.get(sk.get("art"))
        if not rows:
            return img
        aw = max(len(r) for r in rows)
        ah = len(rows)
        # 大小：略高于胶囊，上下各裁掉一点点。太大（1.5 倍）会裁成一堆
        # 认不出的方块，太小又只占右边一角。1.08 倍是能认出形又铺得开的平衡点。
        # 高度略小于胶囊：耳朵、脚、尾焰都得完整，裁掉一截就认不出是谁了。
        cell = max(1, int(self.ch * 0.98 / ah))
        tw, th = aw * cell, ah * cell
        # 横向偏左放：同时压在左边的额度数字和中间的时钟后面，
        # 而不是只占右边一角。
        x0 = min(int(self.cw * 0.16), self.cw - tw)
        y0 = (self.ch - th) // 2
        tint = sk["pet"]
        a = 16 if sum(sk["bg1"]) > 380 else 22       # 浅色皮肤压淡一点
        ov = Image.new("RGBA", (self.cw, self.ch), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        for ry, line in enumerate(rows):
            for rx, ch in enumerate(line):
                if ch == "." or ch == "p":           # 瞳孔留空，眼睛才认得出
                    continue
                px0, py0 = x0 + rx * cell, y0 + ry * cell
                if px0 > self.cw or py0 > self.ch or px0 + cell < 0 or py0 + cell < 0:
                    continue
                od.rectangle([px0, py0, px0 + cell - 1, py0 + cell - 1], fill=tint + (a,))
        return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")

    def _wm_image(self, img, sk, pic):
        """自定义图片版的水印。小图（<=64px）按像素画放大，大图平滑缩放。"""
        pic = pic.convert("RGBA")
        pw, ph = pic.size
        scale = (self.ch * 0.98) / ph
        tw, th = max(1, int(pw * scale)), max(1, int(ph * scale))
        pic = pic.resize((tw, th), Image.NEAREST if max(pw, ph) <= 64 else Image.LANCZOS)
        a = pic.getchannel("A")
        if a.getextrema()[0] == 255:          # 没有透明通道：按亮度抠掉白底
            g = pic.convert("L").point(lambda v: 255 - v)
            a = g.point(lambda v: 255 if v > 40 else 0)
        a = a.point(lambda v: int(v * (0.16 if sum(sk["bg1"]) > 380 else 0.20)))
        pic.putalpha(a)
        x0 = min(int(self.cw * 0.16), max(0, self.cw - tw))
        ov = Image.new("RGBA", (self.cw, self.ch), (0, 0, 0, 0))
        ov.alpha_composite(pic, (x0, (self.ch - th) // 2))
        return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")

    # ── 内容：左边额度，右边时钟 + 会话 + 宠物 ──
    def content(self, st, sk):
        img = self.backdrop(sk).copy()
        d = ImageDraw.Draw(img, "RGBA")
        pad = self.px(17)
        lw = self.px(258)
        rx = pad + lw + self.px(15)
        rw = self.cw - rx - pad
        self._quota(d, st, sk, pad, lw)
        self._right(d, st, sk, rx, rw)
        return img

    def _bar(self, d, sk, x, y, w, h, pct, tick=None):
        r = h // 2
        d.rounded_rectangle([x, y, x + w, y + h], r, fill=sk["track"])
        fw = int(w * min(100.0, max(0.0, pct)) / 100.0)
        if fw > 1:
            col = sk["bad"] if pct >= 90 else sk["warn"] if pct >= 70 else sk["ok"]
            d.rounded_rectangle([x, y, x + max(fw, h), y + h], r, fill=col)
        if tick is not None:
            tx = x + int(w * min(1.0, max(0.0, tick)))
            d.rectangle([tx, y - self.px(2.5), tx + max(1, self.px(1.5)), y + h + self.px(2.5)],
                        fill=sk["ink"])

    def _hero(self, d, sk, x, y, w, row, big_px):
        """一个"主角"格：标签、大号百分比、→预计值、节奏条。"""
        d.text((x, y), flat(row["label"]), font=font(self.px(10.5)), fill=sk["ink2"])
        big = "%d%%" % round(row["pct"])
        fb = font(self.px(big_px), mono=True)
        d.text((x - self.px(1), y + self.px(11)), big, font=fb, fill=sk["ink"])
        bw = d.textlength(big, font=fb)
        if row.get("end") is not None:
            e = row["end"]
            col = sk["bad"] if e >= 100 else sk["warn"] if e >= 85 else sk["ok"]
            f2 = font(self.px(11), mono=True)
            d.text((x + bw + self.px(6), y + self.px(11 + big_px * 0.52)),
                   "→%d%%" % min(999, round(e)), font=f2, fill=col)
        by = y + self.px(11 + big_px * 1.15)
        self._bar(d, sk, x, by, w, self.px(6), row["pct"], row.get("tick"))
        return by + self.px(6)

    def _quota(self, d, st, sk, x, w):
        rows = st.get("quota") or []
        if not rows:
            d.text((x, self.px(16)), "额度", font=font(self.px(11)), fill=sk["ink3"])
            d.text((x, self.px(36)), "等待上报", font=font(self.px(12)), fill=sk["ink3"])
            return
        # 主角：本周总量 + 按模型拆分的那格（真正先卡住人的是它）。
        # 两个都在就并排各占一半；只有一个就独占整行。5 小时降成底下一条细行。
        week = next((r for r in rows if r.get("key") == "seven_day"), None)
        scoped = [r for r in rows if str(r.get("key", "")).startswith("seven_day_")]
        scoped.sort(key=lambda r: -r["pct"])
        heroes = [r for r in (week, scoped[0] if scoped else None) if r]
        if not heroes:
            heroes = rows[:1]
        rest = [r for r in rows if r not in heroes][:2]

        y = self.px(13)
        if len(heroes) == 2:
            gap = self.px(14)
            cw = (w - gap) // 2
            b1 = self._hero(d, sk, x, y, cw, heroes[0], 26)
            b2 = self._hero(d, sk, x + cw + gap, y, cw, heroes[1], 26)
            bottom = max(b1, b2)
        else:
            bottom = self._hero(d, sk, x, y, w, heroes[0], 32)

        # 倒计时：两个周窗口同时重置，写一次就够
        left = next((r.get("left") for r in heroes if r.get("left")), None)
        ty = bottom + self.px(6)
        if left:
            lf = "本周还有 " + flat(left)
            d.text((x, ty), lf, font=font(self.px(9.5)), fill=sk["ink3"])
        ry = ty + self.px(17)

        for r0 in rest:
            d.text((x, ry + self.px(1)), flat(r0["label"]),
                   font=font(self.px(10)), fill=sk["ink3"])
            fp = font(self.px(12), mono=True)
            pct = "%d%%" % round(r0["pct"])
            d.text((x + self.px(56) - d.textlength(pct, font=fp), ry - self.px(1)),
                   pct, font=fp, fill=sk["ink2"])
            bx = x + self.px(64)
            bw2 = w - self.px(64) - self.px(58)
            self._bar(d, sk, bx, ry + self.px(4), bw2, self.px(4), r0["pct"], r0.get("tick"))
            if r0.get("left"):
                lf = flat(r0["left"])
                f4 = numfont(self.px(9.5), lf)
                d.text((x + w - d.textlength(lf, font=f4), ry + self.px(1)),
                       lf, font=f4, fill=sk["ink3"])
            ry += self.px(17)

    def _right(self, d, st, sk, x, w):
        pom = st.get("pom") or {}
        y = self.px(14)
        d.text((x, y), flat(pom.get("clock")) or "—",
               font=font(self.px(25), mono=True), fill=sk["ink"])
        d.text((x + self.px(1), y + self.px(30)),
               (flat(pom.get("phase")) or "未运行") + " · 今日 %s" % (pom.get("done") or 0),
               font=font(self.px(9)), fill=sk["ink3"])
        self._pet(d, st, sk, x + w - self.px(40), self.px(10))

        rows = (st.get("sessions") or [])[:3]
        ry = y + self.px(48)
        if st.get("kb_lost"):
            d.text((x, ry), "键盘未连接 · 点这里去授权", font=font(self.px(10.5), bold=True),
                   fill=sk["warn"])
            ry += self.px(18)
            rows = rows[:2]
        if not rows:
            d.text((x, ry), "没有会话", font=font(self.px(10)), fill=sk["ink3"])
            return
        for s in rows:
            name = "本机" if s.get("key") in ("local", "claude") else flat(s.get("key", ""))
            state = s.get("state", "idle")
            scol = {"idle": sk["ink3"], "busy": sk["info"], "wait": sk["warn"],
                    "done": sk["ok"], "error": sk["bad"]}[state]
            d.ellipse([x, ry + self.px(4), x + self.px(5), ry + self.px(9)], fill=scol)
            cx = x + self.px(11)
            f = font(self.px(10.5), bold=True)
            nm = name
            while nm and d.textlength(nm, font=f) > w - self.px(58):
                nm = nm[:-1]
            d.text((cx, ry), nm, font=f, fill=_tag_color(name))
            cx += d.textlength(nm, font=f) + self.px(7)
            f2 = font(self.px(10))
            d.text((cx, ry + self.px(1)), ST_TXT.get(state, state), font=f2, fill=scol)
            ry += self.px(16)

    def _pet(self, d, st, sk, x, y):
        pet = PETS.get(st.get("petKind") or "octopus", PETS["octopus"])
        state = st.get("pet", "idle")
        phase = st.get("phase", 0)
        rows = list(pet["art"])
        if state == "error":
            rows = _eyes(rows, pet["eyes"], "cross")
        elif state == "done":
            rows = _eyes(rows, pet["eyes"], "happy")
        elif state == "idle" and (phase // 20) % 7 == 0:
            rows = _eyes(rows, pet["eyes"], "blink")
        rows = rows + pet["legs"][(phase // 7) % 2]
        bob = 1 if (state == "busy" and (phase // 5) % 2 == 0) else 0
        k = max(2, self.px(2.2))
        cmap = {"#": sk["pet"], "w": (255, 255, 255), "y": sk["pet2"], "p": sk["bg2"],
                "-": sk["bg2"], "^": sk["bg2"], "x": sk["bg2"]}
        for ry, line in enumerate(rows):
            for rx, ch in enumerate(line):
                c = cmap.get(ch)
                if c:
                    px0, py0 = x + rx * k, y + (ry + bob) * k
                    d.rectangle([px0, py0, px0 + k - 1, py0 + k - 1], fill=c)
        mark = {"wait": [(14, 0), (14, 1), (14, 3)],
                "error": [(13, 0), (15, 0), (14, 1), (13, 2), (15, 2)],
                "done": [(13, 2), (14, 3), (15, 1), (16, 0)],
                "busy": [(13, 1), (14, 1), (15, 1)]}.get(state)
        if mark:
            bc = {"wait": sk["warn"], "error": sk["bad"], "done": sk["ok"],
                  "busy": sk["info"]}[state]
            lim = (phase // 6) % 3 if state == "busy" else 99
            for i, (mx, my) in enumerate(mark):
                if i > lim:
                    break
                d.rectangle([x + mx * k, y + my * k, x + mx * k + k - 1, y + my * k + k - 1],
                            fill=bc)

    # ── 形状：四角全圆的悬浮方块 ──
    def rect(self, style, t):
        """返回 (左, 上, 右, 下, 圆角, 透明度, 内容偏移)。坐标在画布里。"""
        R = self.px(34)
        cx = self.W // 2
        fx0, fx1 = self.M, self.M + self.cw
        fy0, fy1 = self.T, self.T + self.ch
        if style == "drop":
            dy = int(-(1 - t) * (self.T + self.ch))
            return fx0, fy0 + dy, fx1, fy1 + dy, R, 1.0, (0, dy)
        if style == "unfold":
            h = max(self.px(8), int(self.ch * t))
            cy = (fy0 + fy1) // 2
            return fx0, cy - h // 2, fx1, cy + h // 2, min(R, h // 2), 1.0, (0, 0)
        if style == "fade":
            dy = int(-(1 - t) * self.px(14))
            return fx0, fy0 + dy, fx1, fy1 + dy, R, max(0.0, min(1.0, t * 1.2)), (0, dy)
        # island：从上沿下面一颗小药丸长开
        pw, ph = self.px(150), self.px(34)
        w = int(pw + (self.cw - pw) * t)
        h = int(ph + (self.ch - ph) * t)
        r = min(h // 2, int(R * t + ph / 2.0 * (1 - t)))
        y0 = self.T
        return cx - w // 2, y0, cx + w // 2, y0 + h, r, 1.0, (0, 0)

    def frame(self, body, style, t, _radius=None):
        x0, y0, x1, y1, r, alpha, (ox, oy) = self.rect(style, t)
        mask = Image.new("L", (self.W, self.H), 0)
        ImageDraw.Draw(mask).rounded_rectangle([x0, y0, x1, y1], r, fill=255)
        if alpha < 0.999:
            mask = mask.point(lambda v: int(v * alpha))
        canvas = Image.new("RGB", (self.W, self.H), (0, 0, 0))
        canvas.paste(body, (self.M + ox, self.T + oy))
        r_, g_, b_ = canvas.split()
        # 遮罩乘 RGB 得到的正好是预乘 alpha，省掉贴图前再乘一遍
        return Image.merge("RGBA", (ImageChops.multiply(b_, mask),
                                    ImageChops.multiply(g_, mask),
                                    ImageChops.multiply(r_, mask),
                                    mask)).tobytes("raw", "RGBA")
