// ATK YOGO 75 PRO —— 6x6 点阵屏图案库
// 屏幕: 6 行 x 6 列 全彩 LED, 行0=上, 列0=左 (已实测校准)
// 字符 -> RGB。'.' = 熄灭
const PALETTE = {
  '.': [0, 0, 0],
  r: [255, 40, 30],    // 红
  o: [255, 120, 0],    // 橙
  y: [255, 210, 0],    // 黄
  g: [40, 220, 60],    // 绿
  c: [0, 200, 255],    // 青
  b: [40, 90, 255],    // 蓝
  p: [255, 60, 170],   // 粉
  v: [160, 60, 255],   // 紫
  w: [255, 255, 255],  // 白
  d: [90, 90, 110],    // 暗灰 (勾边用)
};

const DESIGNS = {
  tomato:   ['..gg..', '.rrrr.', 'rrrrrr', 'rrrrrr', '.rrrr.', '..rr..'],
  heart:    ['rr..rr', 'rrrrrr', 'rrrrrr', '.rrrr.', '..rr..', '......'],
  smiley:   ['.yyyy.', 'yyyyyy', 'y.yy.y', 'yyyyyy', 'y....y', '.yyyy.'],
  cat:      ['o....o', 'oooooo', 'o.oo.o', 'ooppoo', 'oooooo', '.oooo.'],
  skull:    ['.wwww.', 'wwwwww', 'w.ww.w', 'ww..ww', '.wwww.', '.w.w.w'],
  bolt:     ['...yy.', '..yy..', '.yyyy.', '..yy..', '.yy...', '.y....'],
  coffee:   ['.d.d..', '..d.d.', 'wwwww.', 'wwwwww', 'wwwww.', '.www..'],
  note:     ['....cc', '...c.c', '...c..', '...c..', '.ccc..', '.ccc..'],
  star:     ['..y...', '..y...', 'yyyyyy', '.yyyy.', '.y..y.', 'y....y'],
  ghost:    ['.cccc.', 'cccccc', 'c.cc.c', 'cccccc', 'cccccc', 'c.c.c.'],
  flower:   ['.p..p.', 'pp..pp', '..yy..', '..yy..', 'g.gg.g', '.gggg.'],
  invader:  ['g.g..g', '.gggg.', 'gg..gg', 'gggggg', '.g..g.', 'g.gg.g'],
  fire:     ['...o..', '..oo..', '.oyyo.', 'oyyyyo', 'oyrryo', '.orro.'],
  moon:     ['..yyy.', '.yy...', 'yy....', 'yy....', '.yy...', '..yyy.'],
  bulb:     ['..yy..', '.yyyy.', 'yyyyyy', '.yyyy.', '..yy..', '..dd..'],
  book:     ['bbb.bb', 'bwwbww', 'bwwbww', 'bwwbww', 'bwwbww', 'bbb.bb'],
  frame:    ['oooooo', 'o....o', 'o.bb.o', 'o.bb.o', 'o....o', 'oooooo'],
  check:    ['.....g', '....g.', 'g..g..', '.g.g..', '..gg..', '......'],
  cross:    ['r....r', '.r..r.', '..rr..', '..rr..', '.r..r.', 'r....r'],
  wave:     ['......', '.c..c.', 'c.cc.c', '......', '.b..b.', 'b.bb.b'],
};

module.exports = { PALETTE, DESIGNS };
