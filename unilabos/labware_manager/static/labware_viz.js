/**
 * labware_viz.js — PRCXI 耗材 SVG 2D 可视化渲染引擎
 *
 * renderTopDown(container, itemData)  — 俯视图
 * renderSideProfile(container, itemData) — 侧面截面图
 */

const TYPE_COLORS = {
    plate: '#3b82f6',
    tip_rack: '#10b981',
    tube_rack: '#f59e0b',
    trash: '#ef4444',
    plate_adapter: '#8b5cf6',
};

function _svgNS() { return 'http://www.w3.org/2000/svg'; }

function _makeSVG(w, h) {
    const svg = document.createElementNS(_svgNS(), 'svg');
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.setAttribute('width', '100%');
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    svg.style.background = '#fff';
    return svg;
}

function _rect(svg, x, y, w, h, fill, stroke, rx) {
    const r = document.createElementNS(_svgNS(), 'rect');
    r.setAttribute('x', x); r.setAttribute('y', y);
    r.setAttribute('width', w); r.setAttribute('height', h);
    r.setAttribute('fill', fill || 'none');
    r.setAttribute('stroke', stroke || '#333');
    r.setAttribute('stroke-width', '0.5');
    if (rx) r.setAttribute('rx', rx);
    svg.appendChild(r);
    return r;
}

function _circle(svg, cx, cy, r, fill, stroke) {
    const c = document.createElementNS(_svgNS(), 'circle');
    c.setAttribute('cx', cx); c.setAttribute('cy', cy);
    c.setAttribute('r', r);
    c.setAttribute('fill', fill || 'none');
    c.setAttribute('stroke', stroke || '#333');
    c.setAttribute('stroke-width', '0.4');
    svg.appendChild(c);
    return c;
}

function _text(svg, x, y, txt, size, anchor, fill) {
    const t = document.createElementNS(_svgNS(), 'text');
    t.setAttribute('x', x); t.setAttribute('y', y);
    t.setAttribute('font-size', size || '3');
    t.setAttribute('text-anchor', anchor || 'middle');
    t.setAttribute('fill', fill || '#666');
    t.setAttribute('font-family', 'sans-serif');
    t.textContent = txt;
    svg.appendChild(t);
    return t;
}

function _line(svg, x1, y1, x2, y2, stroke, dash) {
    const l = document.createElementNS(_svgNS(), 'line');
    l.setAttribute('x1', x1); l.setAttribute('y1', y1);
    l.setAttribute('x2', x2); l.setAttribute('y2', y2);
    l.setAttribute('stroke', stroke || '#999');
    l.setAttribute('stroke-width', '0.3');
    if (dash) l.setAttribute('stroke-dasharray', dash);
    svg.appendChild(l);
    return l;
}

function _title(el, txt) {
    const t = document.createElementNS(_svgNS(), 'title');
    t.textContent = txt;
    el.appendChild(t);
}

// ==================== 俯视图 ====================
function renderTopDown(container, data) {
    container.innerHTML = '';
    if (!data) return;

    const pad = 18;
    const sx = data.size_x || 127;
    const sy = data.size_y || 85;
    const w = sx + pad * 2;
    const h = sy + pad * 2;
    const svg = _makeSVG(w, h);

    const color = TYPE_COLORS[data.type] || '#3b82f6';
    const lightColor = color + '22';

    // 板子外轮廓
    _rect(svg, pad, pad, sx, sy, lightColor, color, 3);

    // 尺寸标注
    _text(svg, pad + sx / 2, pad - 4, `${sx} mm`, '3.5', 'middle', '#333');
    // Y 尺寸 (竖直)
    const yt = document.createElementNS(_svgNS(), 'text');
    yt.setAttribute('x', pad - 5);
    yt.setAttribute('y', pad + sy / 2);
    yt.setAttribute('font-size', '3.5');
    yt.setAttribute('text-anchor', 'middle');
    yt.setAttribute('fill', '#333');
    yt.setAttribute('font-family', 'sans-serif');
    yt.setAttribute('transform', `rotate(-90, ${pad - 5}, ${pad + sy / 2})`);
    yt.textContent = `${sy} mm`;
    svg.appendChild(yt);

    const grid = data.grid;
    const well = data.well;
    const tip = data.tip;
    const tube = data.tube;

    if (grid && (well || tip || tube)) {
        const nx = grid.num_items_x || 1;
        const ny = grid.num_items_y || 1;
        const dx = grid.dx || 0;
        const dy = grid.dy || 0;
        const idx = grid.item_dx || 9;
        const idy = grid.item_dy || 9;

        const child = well || tip || tube;
        const csx = child.size_x || child.spot_size_x || 8;
        const csy = child.size_y || child.spot_size_y || 8;

        const isCircle = well ? (well.cross_section_type === 'CIRCLE') : (!!tip);

        // 行列标签
        for (let col = 0; col < nx; col++) {
            const cx = pad + dx + csx / 2 + col * idx;
            _text(svg, cx, pad + sy + 5, String(col + 1), '2.5', 'middle', '#999');
        }
        const rowLabels = 'ABCDEFGHIJKLMNOP';
        for (let row = 0; row < ny; row++) {
            const cy = pad + dy + csy / 2 + row * idy;
            _text(svg, pad - 4, cy + 1, rowLabels[row] || String(row), '2.5', 'middle', '#999');
        }

        // 绘制孔位
        for (let col = 0; col < nx; col++) {
            for (let row = 0; row < ny; row++) {
                const cx = pad + dx + csx / 2 + col * idx;
                const cy = pad + dy + csy / 2 + row * idy;

                let el;
                if (isCircle) {
                    const r = Math.min(csx, csy) / 2;
                    el = _circle(svg, cx, cy, r, '#fff', color);
                } else {
                    el = _rect(svg, cx - csx / 2, cy - csy / 2, csx, csy, '#fff', color);
                }

                const label = (rowLabels[row] || '') + String(col + 1);
                _title(el, `${label}: ${csx}x${csy} mm`);

                // hover 效果
                el.style.cursor = 'pointer';
                el.addEventListener('mouseenter', () => el.setAttribute('fill', color + '44'));
                el.addEventListener('mouseleave', () => el.setAttribute('fill', '#fff'));
            }
        }

        // dx / dy 标注（板边到首个子元素左上角）
        const dimColor = '#e67e22';
        const firstLeft = pad + dx;        // 首列子元素左边 X
        const firstTop  = pad + dy;        // 首行子元素上边 Y
        if (dx > 0.1) {
            // dx: 板左边 → 首列子元素左边，画在第一行子元素中心高度
            const annY = firstTop + csy / 2;
            _line(svg, pad, annY, firstLeft, annY, dimColor, '1,1');
            _line(svg, pad, annY - 2, pad, annY + 2, dimColor);
            _line(svg, firstLeft, annY - 2, firstLeft, annY + 2, dimColor);
            _text(svg, pad + dx / 2, annY - 2, `dx=${dx}`, '2.5', 'middle', dimColor);
        }
        if (dy > 0.1) {
            // dy: 板上边 → 首行子元素上边，画在第一列子元素中心宽度
            const annX = firstLeft + csx / 2;
            _line(svg, annX, pad, annX, firstTop, dimColor, '1,1');
            _line(svg, annX - 2, pad, annX + 2, pad, dimColor);
            _line(svg, annX - 2, firstTop, annX + 2, firstTop, dimColor);
            _text(svg, annX + 4, pad + dy / 2 + 1, `dy=${dy}`, '2.5', 'start', dimColor);
        }
    } else if (data.type === 'plate_adapter' && data.adapter) {
        // 绘制适配器凹槽
        const adp = data.adapter;
        const ahx = adp.adapter_hole_size_x || 127;
        const ahy = adp.adapter_hole_size_y || 85;
        const adx = adp.dx != null ? adp.dx : (sx - ahx) / 2;
        const ady = adp.dy != null ? adp.dy : (sy - ahy) / 2;
        _rect(svg, pad + adx, pad + ady, ahx, ahy, '#f0f0ff', '#8b5cf6', 2);
        _text(svg, pad + adx + ahx / 2, pad + ady + ahy / 2, `${ahx}x${ahy}`, '4', 'middle', '#8b5cf6');
    } else if (data.type === 'trash') {
        // 简单标记
        _text(svg, pad + sx / 2, pad + sy / 2, 'TRASH', '8', 'middle', '#ef4444');
    }

    container.appendChild(svg);
    _enableZoomPan(svg, `0 0 ${w} ${h}`);
}

// ==================== 侧面截面图 ====================
function renderSideProfile(container, data) {
    container.innerHTML = '';
    if (!data) return;

    const pad = 20;
    const sx = data.size_x || 127;
    const sz = data.size_z || 20;

    // 按比例缩放，侧面以 X-Z 面
    const scaleH = Math.max(1, sz / 60); // 让较矮的板子不会太小

    // 计算枪头露出高度（仅 tip_rack）
    const tip = data.tip;
    const grid = data.grid;
    let tipAbove = 0;
    if (data.type === 'tip_rack' && tip) {
        if (tip.tip_above_rack_length != null && tip.tip_above_rack_length > 0) {
            tipAbove = tip.tip_above_rack_length;
        } else if (tip.tip_length && grid) {
            const dz = grid.dz || 0;
            const calc = tip.tip_length - (sz - dz);
            if (calc > 0) tipAbove = calc;
        }
    }

    const drawW = sx;
    const drawH = sz;
    const w = drawW + pad * 2 + 30; // 额外空间给标注
    const h = drawH + tipAbove + pad * 2 + 10;
    const svg = _makeSVG(w, h);

    const color = TYPE_COLORS[data.type] || '#3b82f6';
    const baseY = pad + tipAbove + drawH; // 底部 Y
    const rackTopY = pad + tipAbove; // rack 顶部 Y

    // 板壳矩形
    _rect(svg, pad, rackTopY, drawW, drawH, color + '15', color);

    // 尺寸标注
    // X 方向
    _line(svg, pad, baseY + 5, pad + drawW, baseY + 5, '#333');
    _text(svg, pad + drawW / 2, baseY + 12, `${sx} mm`, '3.5', 'middle', '#333');

    // Z 方向
    _line(svg, pad + drawW + 5, rackTopY, pad + drawW + 5, baseY, '#333');
    const zt = document.createElementNS(_svgNS(), 'text');
    zt.setAttribute('x', pad + drawW + 12);
    zt.setAttribute('y', rackTopY + drawH / 2);
    zt.setAttribute('font-size', '3.5');
    zt.setAttribute('text-anchor', 'middle');
    zt.setAttribute('fill', '#333');
    zt.setAttribute('font-family', 'sans-serif');
    zt.setAttribute('transform', `rotate(-90, ${pad + drawW + 12}, ${rackTopY + drawH / 2})`);
    zt.textContent = `${sz} mm`;
    svg.appendChild(zt);

    const well = data.well;
    const tube = data.tube;

    if (grid && (well || tip || tube)) {
        const dx = grid.dx || 0;
        const dz = grid.dz || 0;
        const idx = grid.item_dx || 9;
        const nx = Math.min(grid.num_items_x || 1, 24); // 最多画24列
        const dimColor = '#e67e22';

        const child = well || tube;
        const childTip = tip;

        if (child) {
            const csx = child.size_x || 8;
            const csz = child.size_z || 10;
            const bt = well ? (well.bottom_type || 'FLAT') : 'FLAT';

            // 画几个代表性的孔截面
            const nDraw = Math.min(nx, 12);
            for (let i = 0; i < nDraw; i++) {
                const cx = pad + dx + csx / 2 + i * idx;
                const topZ = baseY - dz - csz;
                const botZ = baseY - dz;

                // 孔壁
                _rect(svg, cx - csx / 2, topZ, csx, csz, '#e0e8ff', color, 0.5);

                // 底部形状
                if (bt === 'V') {
                    // V 底 三角
                    const triH = Math.min(csx / 2, csz * 0.3);
                    const p = document.createElementNS(_svgNS(), 'polygon');
                    p.setAttribute('points',
                        `${cx - csx / 2},${botZ - triH} ${cx},${botZ} ${cx + csx / 2},${botZ - triH}`);
                    p.setAttribute('fill', color + '33');
                    p.setAttribute('stroke', color);
                    p.setAttribute('stroke-width', '0.3');
                    svg.appendChild(p);
                } else if (bt === 'U') {
                    // U 底 圆弧
                    const arcR = csx / 2;
                    const p = document.createElementNS(_svgNS(), 'path');
                    p.setAttribute('d', `M ${cx - csx / 2} ${botZ - arcR} A ${arcR} ${arcR} 0 0 0 ${cx + csx / 2} ${botZ - arcR}`);
                    p.setAttribute('fill', color + '33');
                    p.setAttribute('stroke', color);
                    p.setAttribute('stroke-width', '0.3');
                    svg.appendChild(p);
                }
            }

            // dz 标注
            if (dz > 0) {
                const lx = pad + dx + 0.5 * idx * nDraw + csx / 2 + 5;
                _line(svg, lx, baseY, lx, baseY - dz, dimColor, '1,1');
                _line(svg, lx - 2, baseY, lx + 2, baseY, dimColor);
                _line(svg, lx - 2, baseY - dz, lx + 2, baseY - dz, dimColor);
                _text(svg, lx + 4, baseY - dz / 2 + 1, `dz=${dz}`, '2.5', 'start', dimColor);
            }
            // dx 标注
            if (dx > 0.1) {
                const annY = rackTopY + 4;
                _line(svg, pad, annY, pad + dx, annY, dimColor, '1,1');
                _line(svg, pad, annY - 2, pad, annY + 2, dimColor);
                _line(svg, pad + dx, annY - 2, pad + dx, annY + 2, dimColor);
                _text(svg, pad + dx / 2, annY - 2, `dx=${dx}`, '2.5', 'middle', dimColor);
            }
        }

        if (childTip) {
            // 枪头截面
            const tipLen = childTip.tip_length || 50;
            const nDraw = Math.min(nx, 12);
            for (let i = 0; i < nDraw; i++) {
                const cx = pad + dx + 3.5 + i * idx;
                // 枪头顶部 = rack顶部 - 露出长度
                const tipTopZ = rackTopY - tipAbove;
                const drawLen = Math.min(tipLen, sz - dz + tipAbove);

                // 枪头轮廓 (梯形)
                const topW = 4;
                const botW = 1.5;
                const p = document.createElementNS(_svgNS(), 'polygon');
                p.setAttribute('points',
                    `${cx - topW / 2},${tipTopZ} ${cx + topW / 2},${tipTopZ} ${cx + botW / 2},${tipTopZ + drawLen} ${cx - botW / 2},${tipTopZ + drawLen}`);
                p.setAttribute('fill', '#10b98133');
                p.setAttribute('stroke', '#10b981');
                p.setAttribute('stroke-width', '0.3');
                svg.appendChild(p);
            }

            // dz 标注
            if (dz > 0) {
                const lx = pad + dx + nDraw * idx + 5;
                _line(svg, lx, baseY, lx, baseY - dz, dimColor, '1,1');
                _line(svg, lx - 2, baseY, lx + 2, baseY, dimColor);
                _line(svg, lx - 2, baseY - dz, lx + 2, baseY - dz, dimColor);
                _text(svg, lx + 4, baseY - dz / 2 + 1, `dz=${dz}`, '2.5', 'start', dimColor);
            }
            // dx 标注
            if (dx > 0.1) {
                const annY = rackTopY + 4;
                _line(svg, pad, annY, pad + dx, annY, dimColor, '1,1');
                _line(svg, pad, annY - 2, pad, annY + 2, dimColor);
                _line(svg, pad + dx, annY - 2, pad + dx, annY + 2, dimColor);
                _text(svg, pad + dx / 2, annY - 2, `dx=${dx}`, '2.5', 'middle', dimColor);
            }

            // 露出长度标注线
            if (tipAbove > 0) {
                const annotX = pad + dx + nDraw * idx + 8;
                // rack 顶部水平参考线
                _line(svg, annotX - 3, rackTopY, annotX + 3, rackTopY, '#10b981');
                // 枪头顶部水平参考线
                _line(svg, annotX - 3, rackTopY - tipAbove, annotX + 3, rackTopY - tipAbove, '#10b981');
                // 竖直标注线
                _line(svg, annotX, rackTopY - tipAbove, annotX, rackTopY, '#10b981', '1,1');
                _text(svg, annotX + 2, rackTopY - tipAbove / 2 + 1, `露出=${Math.round(tipAbove * 100) / 100}mm`, '2.5', 'start', '#10b981');
            }
        }
    } else if (data.type === 'plate_adapter' && data.adapter) {
        const adp = data.adapter;
        const ahz = adp.adapter_hole_size_z || 10;
        const adz = adp.dz || 0;
        const adx_val = adp.dx != null ? adp.dx : (sx - (adp.adapter_hole_size_x || 127)) / 2;
        const ahx = adp.adapter_hole_size_x || 127;

        // 凹槽截面
        _rect(svg, pad + adx_val, rackTopY + adz, ahx, ahz, '#ede9fe', '#8b5cf6');
        _text(svg, pad + adx_val + ahx / 2, rackTopY + adz + ahz / 2 + 1, `hole: ${ahz}mm deep`, '3', 'middle', '#8b5cf6');
    } else if (data.type === 'trash') {
        _text(svg, pad + drawW / 2, rackTopY + drawH / 2, 'TRASH', '8', 'middle', '#ef4444');
    }

    container.appendChild(svg);
    _enableZoomPan(svg, `0 0 ${w} ${h}`);
}

// ==================== 缩放 & 平移 ====================
function _enableZoomPan(svgEl, origViewBox) {
    const parts = origViewBox.split(' ').map(Number);
    let vx = parts[0], vy = parts[1], vw = parts[2], vh = parts[3];
    const origVx = vx, origVy = vy, origW = vw, origH = vh;
    const MIN_SCALE = 0.5, MAX_SCALE = 5;

    function applyViewBox() {
        svgEl.setAttribute('viewBox', `${vx} ${vy} ${vw} ${vh}`);
    }

    function resetView() {
        vx = origVx; vy = origVy; vw = origW; vh = origH;
        applyViewBox();
    }

    // 将 resetView 挂到 svg 元素上，方便外部调用
    svgEl._resetView = resetView;

    svgEl.addEventListener('wheel', function (e) {
        e.preventDefault();
        if (e.ctrlKey) {
            // pinch / ctrl+scroll → 缩放
            const factor = e.deltaY > 0 ? 1.08 : 1 / 1.08;
            const newW = vw * factor;
            const newH = vh * factor;
            // 限制缩放范围
            if (newW < origW / MAX_SCALE || newW > origW * (1 / MIN_SCALE)) return;
            // 以鼠标位置为缩放中心
            const rect = svgEl.getBoundingClientRect();
            const mx = (e.clientX - rect.left) / rect.width;
            const my = (e.clientY - rect.top) / rect.height;
            vx += (vw - newW) * mx;
            vy += (vh - newH) * my;
            vw = newW;
            vh = newH;
        } else {
            // 普通滚轮 → 平移
            const panSpeed = vw * 0.002;
            vx += e.deltaX * panSpeed;
            vy += e.deltaY * panSpeed;
        }
        applyViewBox();
    }, { passive: false });
}

// 回中按钮：重置指定容器内 SVG 的 viewBox
function resetSvgView(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const svg = container.querySelector('svg');
    if (svg && svg._resetView) svg._resetView();
}
