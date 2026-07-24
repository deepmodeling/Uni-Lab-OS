/**
 * form_handler.js — 动态表单逻辑 + 实时预览
 */

// 根据类型显示/隐藏对应的表单段
function onTypeChange() {
    const type = document.getElementById('f-type').value;
    const sections = {
        grid: ['plate', 'tip_rack', 'tube_rack'],
        well: ['plate'],
        tip: ['tip_rack'],
        tube: ['tube_rack'],
        adapter: ['plate_adapter'],
    };

    for (const [sec, types] of Object.entries(sections)) {
        const el = document.getElementById('section-' + sec);
        if (el) el.style.display = types.includes(type) ? 'block' : 'none';
    }

    // plate_type 行只对 plate 显示
    const ptRow = document.getElementById('row-plate_type');
    if (ptRow) ptRow.style.display = type === 'plate' ? 'block' : 'none';

    updatePreview();
}

// 从表单收集数据
function collectFormData() {
    const g = id => {
        const el = document.getElementById(id);
        if (!el) return null;
        if (el.type === 'checkbox') return el.checked;
        if (el.type === 'number') return el.value === '' ? null : parseFloat(el.value);
        return el.value || null;
    };

    const type = g('f-type');

    const data = {
        type: type,
        function_name: g('f-function_name') || 'PRCXI_new',
        model: g('f-model'),
        docstring: g('f-docstring') || '',
        plate_type: type === 'plate' ? g('f-plate_type') : null,
        size_x: g('f-size_x') || 127,
        size_y: g('f-size_y') || 85,
        size_z: g('f-size_z') || 20,
        material_info: {
            uuid: g('f-mi_uuid') || '',
            Code: g('f-mi_code') || '',
            Name: g('f-mi_name') || '',
            materialEnum: g('f-mi_menum'),
            SupplyType: g('f-mi_stype'),
        },
        registry_category: (g('f-reg_cat') || 'prcxi,plates').split(',').map(s => s.trim()),
        registry_description: g('f-reg_desc') || '',
        include_in_template_matching: g('f-in_tpl') || false,
        template_kind: g('f-tpl_kind') || null,
        grid: null,
        well: null,
        tip: null,
        tube: null,
        adapter: null,
        volume_functions: null,
    };

    // Grid
    if (['plate', 'tip_rack', 'tube_rack'].includes(type)) {
        data.grid = {
            num_items_x: g('f-grid_nx') || 12,
            num_items_y: g('f-grid_ny') || 8,
            dx: g('f-grid_dx') || 0,
            dy: g('f-grid_dy') || 0,
            dz: g('f-grid_dz') || 0,
            item_dx: g('f-grid_idx') || 9,
            item_dy: g('f-grid_idy') || 9,
        };
    }

    // Well
    if (type === 'plate') {
        data.well = {
            size_x: g('f-well_sx') || 8,
            size_y: g('f-well_sy') || 8,
            size_z: g('f-well_sz') || 10,
            max_volume: g('f-well_vol'),
            material_z_thickness: g('f-well_mzt'),
            bottom_type: g('f-well_bt') || 'FLAT',
            cross_section_type: g('f-well_cs') || 'CIRCLE',
        };
        if (g('f-has_vf')) {
            data.volume_functions = {
                type: 'rectangle',
                well_length: data.well.size_x,
                well_width: data.well.size_y,
            };
        }
    }

    // Tip
    if (type === 'tip_rack') {
        data.tip = {
            spot_size_x: g('f-tip_sx') || 7,
            spot_size_y: g('f-tip_sy') || 7,
            spot_size_z: g('f-tip_sz') || 0,
            tip_volume: g('f-tip_vol') || 300,
            tip_length: g('f-tip_len') || 60,
            tip_fitting_depth: g('f-tip_dep') || 51,
            tip_above_rack_length: g('f-tip_above'),
            has_filter: g('f-tip_filter') || false,
        };
    }

    // Tube
    if (type === 'tube_rack') {
        data.tube = {
            size_x: g('f-tube_sx') || 10.6,
            size_y: g('f-tube_sy') || 10.6,
            size_z: g('f-tube_sz') || 40,
            max_volume: g('f-tube_vol') || 1500,
        };
    }

    // Adapter
    if (type === 'plate_adapter') {
        data.adapter = {
            adapter_hole_size_x: g('f-adp_hsx') || 127.76,
            adapter_hole_size_y: g('f-adp_hsy') || 85.48,
            adapter_hole_size_z: g('f-adp_hsz') || 10,
            dx: g('f-adp_dx'),
            dy: g('f-adp_dy'),
            dz: g('f-adp_dz') || 0,
        };
    }

    return data;
}

// 实时预览 (debounce)
let _previewTimer = null;
function updatePreview() {
    if (_previewTimer) clearTimeout(_previewTimer);
    _previewTimer = setTimeout(() => {
        const data = collectFormData();
        const topEl = document.getElementById('svg-topdown');
        const sideEl = document.getElementById('svg-side');
        if (topEl) renderTopDown(topEl, data);
        if (sideEl) renderSideProfile(sideEl, data);
    }, 200);
}

// 给所有表单元素绑定 input 事件
document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('labware-form');
    if (!form) return;
    form.addEventListener('input', updatePreview);
    form.addEventListener('change', updatePreview);

    // tip_above_rack_length 与 dz 互算
    // 公式: tip_length = tip_above_rack_length + size_z - dz
    // 规则: 填 tip_above → 自动算 dz；填 dz → 自动算 tip_above
    //       改 size_z / tip_length → 优先重算 tip_above（若有值），否则算 dz

    function _getVal(id) {
        const el = document.getElementById(id);
        return (el && el.value !== '') ? parseFloat(el.value) : null;
    }
    function _setVal(id, v) {
        const el = document.getElementById(id);
        if (el) el.value = Math.round(v * 1000) / 1000;
    }

    function autoCalcTipAbove(changedId) {
        const typeEl = document.getElementById('f-type');
        if (!typeEl || typeEl.value !== 'tip_rack') return;

        const tipLen = _getVal('f-tip_len');
        const sizeZ  = _getVal('f-size_z');
        const dz     = _getVal('f-grid_dz');
        const above  = _getVal('f-tip_above');

        // 需要 tip_length 和 size_z 才能计算
        if (tipLen == null || sizeZ == null) return;

        if (changedId === 'f-tip_above') {
            // 用户填了 tip_above → 算 dz
            if (above != null) _setVal('f-grid_dz', above + sizeZ - tipLen);
        } else if (changedId === 'f-grid_dz') {
            // 用户填了 dz → 算 tip_above
            if (dz != null) _setVal('f-tip_above', tipLen - sizeZ + dz);
        } else {
            // size_z 或 tip_length 变了 → 优先重算 tip_above（若已有值或 dz 已有值）
            if (dz != null) {
                _setVal('f-tip_above', tipLen - sizeZ + dz);
            } else if (above != null) {
                _setVal('f-grid_dz', above + sizeZ - tipLen);
            }
        }
    }

    // 绑定 input 事件
    for (const id of ['f-tip_len', 'f-size_z', 'f-grid_dz', 'f-tip_above']) {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', () => autoCalcTipAbove(id));
    }

    // 编辑已有 tip_rack 条目时自动补算 tip_above_rack_length
    const typeEl = document.getElementById('f-type');
    if (typeEl && typeEl.value === 'tip_rack') {
        autoCalcTipAbove('f-grid_dz');
    }
});

// 自动居中：根据板尺寸和孔阵列参数计算 dx/dy
function autoCenter() {
    const g = id => { const el = document.getElementById(id); return el && el.value !== '' ? parseFloat(el.value) : 0; };

    const sizeX = g('f-size_x') || 127;
    const sizeY = g('f-size_y') || 85;
    const nx = g('f-grid_nx') || 1;
    const ny = g('f-grid_ny') || 1;
    const itemDx = g('f-grid_idx') || 9;
    const itemDy = g('f-grid_idy') || 9;

    // 根据当前耗材类型确定子元素尺寸
    const type = document.getElementById('f-type').value;
    let childSx = 0, childSy = 0;
    if (type === 'plate') {
        childSx = g('f-well_sx') || 8;
        childSy = g('f-well_sy') || 8;
    } else if (type === 'tip_rack') {
        childSx = g('f-tip_sx') || 7;
        childSy = g('f-tip_sy') || 7;
    } else if (type === 'tube_rack') {
        childSx = g('f-tube_sx') || 10.6;
        childSy = g('f-tube_sy') || 10.6;
    }

    // dx = (板宽 - 孔阵列总占宽) / 2
    const dx = (sizeX - (nx - 1) * itemDx - childSx) / 2;
    const dy = (sizeY - (ny - 1) * itemDy - childSy) / 2;

    const elDx = document.getElementById('f-grid_dx');
    const elDy = document.getElementById('f-grid_dy');
    if (elDx) elDx.value = Math.round(dx * 100) / 100;
    if (elDy) elDy.value = Math.round(dy * 100) / 100;

    updatePreview();
}

// 保存
function showMsg(text, ok) {
    const el = document.getElementById('status-msg');
    if (!el) return;
    el.textContent = text;
    el.className = 'status-msg ' + (ok ? 'msg-ok' : 'msg-err');
    el.style.display = 'block';
    setTimeout(() => el.style.display = 'none', 4000);
}

async function saveForm() {
    const data = collectFormData();

    let url, method;
    if (typeof IS_NEW !== 'undefined' && IS_NEW) {
        url = '/api/labware';
        method = 'POST';
    } else {
        url = '/api/labware/' + (typeof ITEM_ID !== 'undefined' ? ITEM_ID : '');
        method = 'PUT';
    }

    try {
        const r = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        const d = await r.json();
        if (d.status === 'ok') {
            showMsg('保存成功', true);
            if (IS_NEW) {
                setTimeout(() => location.href = '/labware/' + data.function_name, 500);
            }
        } else {
            showMsg('保存失败: ' + JSON.stringify(d), false);
        }
    } catch (e) {
        showMsg('请求错误: ' + e.message, false);
    }
}
