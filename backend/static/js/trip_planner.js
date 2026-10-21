/* ==========================================================================
 * trip_planner.js —— 智慧景区·行程规划
 * 三页：规划页 / 结果页 / 历史列表
 * 入口：openTripPlanner() —— 由 index.html 的「行程规划」按钮调用
 * 数据：全部来自后端（生成/历史/详情/地图），本地不保留任何景点数据
 *       接口挂了就如实报错，不做本地兜底
 * 地图：依赖 Leaflet 与后端 /api/trip/map 返回的点位数据
 * ========================================================================== */
(function () {
    'use strict';

    const STYLE_ID = 'trip-planner-style';
    const ROOT_ID = 'tripPlannerRoot';

    const API_PLAN = '/api/trip/plan';
    const API_HISTORY = '/api/trip/history';
    const API_MAP = id => '/api/trip/map/' + encodeURIComponent(id);

    /* --------------------------- 请求封装 --------------------------- */
    // 统一把后端的 {detail} 抛成 Error，调用方负责展示
    async function request(url, options) {
        let res;
        try {
            res = await fetch(url, options);
        } catch (e) {
            throw new Error('无法连接服务端，请确认后端已启动');
        }
        const text = await res.text();
        let data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { /* 非 JSON */ }
        if (!res.ok) {
            const msg = (data && (data.detail || data.message)) || ('请求失败（HTTP ' + res.status + '）');
            throw new Error(msg);
        }
        return data;
    }

    /* ------------------------------ 样式 ------------------------------ */
    const CSS = `
    #${ROOT_ID} { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; }
    #${ROOT_ID} * { box-sizing: border-box; }
    .tp-root {
        flex: 1; padding: 14px 20px 24px; overflow: auto;
        background: linear-gradient(180deg, #F2F8FE 0%, #FFFFFF 40%);
        min-height: calc(100vh - 64px);
    }
    .tp-shell { width: 100%; max-width: none; margin: 0; }

    /* 步骤条 + 返回按钮 */
    .tp-steps { display: flex; gap: 10px; align-items: stretch; margin: 0 0 4px; width: 100%; }
    .tp-step {
        flex: 1; display: flex; align-items: center; gap: 10px; padding: 10px 16px;
        background: #fff; border: 1px solid #E1EEFB; border-radius: 12px; cursor: pointer;
        transition: all .2s;
    }
    .tp-back-pill {
        flex-shrink: 0; border: 1px solid #C9E2FA; background: #fff; color: #1565C0;
        border-radius: 24px; padding: 8px 18px; font-size: 13px; font-weight: 600; cursor: pointer;
        display: flex; align-items: center; gap: 7px; transition: all .2s; font-family: inherit;
        align-self: center;
    }
    .tp-back-pill:hover { background: #1E88E5; color: #fff; border-color: #1E88E5; }
    .tp-step:hover { border-color: #90CAF9; }
    .tp-step.active { border-color: #1E88E5; background: #EAF4FE; box-shadow: 0 3px 10px rgba(30,136,229,.12); }
    .tp-step.disabled { opacity: .45; cursor: not-allowed; }
    .tp-step-no {
        width: 24px; height: 24px; border-radius: 50%; background: #E3F2FD; color: #1565C0;
        font-size: 13px; font-weight: 700; display: flex; align-items: center; justify-content: center;
    }
    .tp-step.active .tp-step-no { background: #1E88E5; color: #fff; }
    .tp-step-name { font-size: 14px; color: #33475B; font-weight: 600; }
    .tp-step-desc { font-size: 11px; color: #8A9AAE; margin-top: 1px; }

    .tp-card {
        background: #fff; border: 1px solid #E1EEFB; border-radius: 16px;
        padding: 20px 22px; margin-top: 16px; box-shadow: 0 3px 14px rgba(30,136,229,.05);
    }
    .tp-card-title {
        font-size: 15px; font-weight: 700; color: #1565C0;
        display: flex; align-items: center; gap: 8px; margin-bottom: 14px;
        padding-bottom: 10px; border-bottom: 1px dashed #DCEAF8;
    }
    .tp-card-title .tp-badge {
        font-size: 11px; font-weight: 500; color: #5A7A96; background: #EAF4FE;
        border-radius: 10px; padding: 2px 8px; margin-left: auto;
    }

    /* 表单 */
    .tp-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
    .tp-grid-4 { grid-template-columns: repeat(4, 1fr); }
    .tp-field { display: flex; flex-direction: column; gap: 6px; }
    .tp-field.full { grid-column: 1 / -1; }
    .tp-label { font-size: 13px; color: #546E7A; font-weight: 600; }
    .tp-label i { color: #1E88E5; margin-right: 5px; }
    .tp-input, .tp-select {
        height: 42px; border: 1px solid #D6E6F5; border-radius: 10px; padding: 0 12px;
        font-size: 14px; color: #263238; background: #FBFDFF; outline: none; transition: all .18s;
        font-family: inherit; width: 100%;
    }
    .tp-input:focus, .tp-select:focus { border-color: #1E88E5; box-shadow: 0 0 0 3px rgba(30,136,229,.12); }
    .tp-select { appearance: none; -webkit-appearance: none; cursor: pointer;
        background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path fill='%231565C0' d='M6 8.5L1.5 4h9z'/></svg>");
        background-repeat: no-repeat; background-position: right 12px center; padding-right: 30px; }

    /* 标签多选 */
    .tp-tags { display: flex; flex-wrap: wrap; gap: 8px; }
    .tp-tag {
        border: 1px solid #D6E6F5; background: #FBFDFF; color: #455A64; border-radius: 20px;
        padding: 7px 14px; font-size: 13px; cursor: pointer; user-select: none; transition: all .18s;
        display: flex; align-items: center; gap: 6px;
    }
    .tp-tag:hover { border-color: #90CAF9; }
    .tp-tag.on { background: #1E88E5; border-color: #1E88E5; color: #fff; box-shadow: 0 3px 8px rgba(30,136,229,.25); }

    /* 分段选择 */
    .tp-seg { display: flex; background: #EEF6FD; border-radius: 10px; padding: 4px; gap: 4px; }
    .tp-seg-item {
        flex: 1; text-align: center; padding: 8px 4px; font-size: 13px; color: #546E7A;
        border-radius: 8px; cursor: pointer; transition: all .18s;
    }
    .tp-seg-item.on { background: #fff; color: #1565C0; font-weight: 700; box-shadow: 0 2px 6px rgba(21,101,192,.14); }
    .tp-seg-big { padding: 6px; }
    .tp-seg-big .tp-seg-item { padding: 11px 4px; font-size: 14px; }

    .tp-actions { display: flex; gap: 12px; margin-top: 18px; justify-content: center; }
    .tp-textarea {
        width: 100%; min-height: 88px; border: 1px solid #D6E6F5; border-radius: 10px; padding: 10px 12px;
        font-size: 14px; color: #263238; background: #FBFDFF; outline: none; transition: all .18s;
        font-family: inherit; resize: vertical; line-height: 1.6;
    }
    .tp-textarea:focus { border-color: #1E88E5; box-shadow: 0 0 0 3px rgba(30,136,229,.12); }
    .tp-input-suffix { position: relative; }
    .tp-input-suffix .tp-input { padding-right: 44px; }
    .tp-input-suffix::after {
        content: '元'; position: absolute; right: 14px; top: 50%; transform: translateY(-50%);
        color: #90A4AE; font-size: 13px; pointer-events: none;
    }
    .tp-btn {
        border: none; border-radius: 12px; padding: 13px 26px; font-size: 15px; font-weight: 700;
        cursor: pointer; display: flex; align-items: center; gap: 8px; transition: all .2s; font-family: inherit;
    }
    .tp-btn-primary { background: linear-gradient(135deg,#1E88E5,#1565C0); color: #fff; box-shadow: 0 6px 16px rgba(21,101,192,.28); }
    .tp-btn-primary:hover { transform: translateY(-1px); box-shadow: 0 9px 20px rgba(21,101,192,.34); }
    .tp-btn-ghost { background: #EAF4FE; color: #1565C0; border: 1px solid #C9E2FA; }
    .tp-btn-ghost:hover { background: #DCEAF9; }
    .tp-btn[disabled] { opacity: .5; cursor: not-allowed; }

    /* 结果页概览 */
    .tp-summary { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
    .tp-sum-item {
        background: linear-gradient(160deg,#F5FAFF,#EAF4FE); border: 1px solid #DCEAF8;
        border-radius: 12px; padding: 14px 16px;
    }
    .tp-sum-k { font-size: 12px; color: #78909C; }
    .tp-sum-v { font-size: 19px; font-weight: 700; color: #1565C0; margin-top: 4px; }
    .tp-sum-v small { font-size: 12px; font-weight: 500; color: #607D8B; margin-left: 3px; }

    /* 每日行程 */
    .tp-day { border: 1px solid #E1EEFB; border-radius: 14px; margin-top: 14px; overflow: hidden; }
    .tp-day-head {
        display: flex; align-items: center; gap: 10px; padding: 12px 16px;
        background: linear-gradient(90deg,#E3F2FD,#F5FAFF); border-bottom: 1px solid #E1EEFB;
    }
    .tp-day-no {
        background: #1E88E5; color: #fff; font-size: 12px; font-weight: 700;
        border-radius: 8px; padding: 4px 10px;
    }
    .tp-day-title { font-size: 14px; font-weight: 700; color: #1565C0; }
    .tp-day-meta { font-size: 12px; color: #78909C; margin-left: auto; }
    .tp-timeline { padding: 6px 16px 14px; }
    .tp-slot { display: flex; gap: 14px; padding: 12px 0; border-bottom: 1px dashed #EDF4FA; }
    .tp-slot:last-child { border-bottom: none; }
    .tp-slot-time {
        min-width: 92px; font-size: 13px; font-weight: 700; color: #1E88E5; padding-top: 2px;
    }
    .tp-slot-body { flex: 1; }
    .tp-slot-name { font-size: 14px; font-weight: 600; color: #263238; }
    .tp-slot-desc { font-size: 12.5px; color: #607D8B; margin-top: 3px; line-height: 1.55; }
    .tp-slot-tip {
        display: inline-block; margin-top: 6px; font-size: 11.5px; color: #0277BD;
        background: #E1F5FE; border-radius: 6px; padding: 3px 8px;
    }
    /* 实时校准标记：天气 / 转场 / 开放时间 */
    .tp-day-weather {
        font-size: 11.5px; color: #00695C; background: #E0F2F1; border: 1px solid #B2DFDB;
        border-radius: 10px; padding: 2px 9px; white-space: nowrap;
    }
    .tp-slot-transfer { font-size: 11.5px; color: #78909C; margin-top: 4px; }
    .tp-slot-open {
        display: inline-block; margin-top: 6px; margin-right: 6px; font-size: 11.5px;
        color: #01579B; background: #E1F5FE; border-radius: 6px; padding: 3px 8px; cursor: help;
    }
    .tp-slot-flag {
        display: inline-block; margin-top: 6px; font-size: 11.5px;
        color: #7A5A00; background: #FFF8E1; border-radius: 6px; padding: 3px 8px;
    }
    .tp-slot-flag.warn { color: #A04000; background: #FFF3E0; }

    /* 提示区 */
    .tp-tips { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
    .tp-tip-box { background: #FBFDFF; border: 1px solid #E1EEFB; border-radius: 12px; padding: 14px 16px; }
    .tp-tip-box h5 { font-size: 13px; color: #1565C0; margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
    .tp-tip-box ul { margin: 0; padding-left: 18px; }
    .tp-tip-box li { font-size: 12.5px; color: #546E7A; line-height: 1.8; }

    /* 历史列表 */
    .tp-his-item {
        display: flex; align-items: center; gap: 14px; padding: 14px 16px;
        border: 1px solid #E1EEFB; border-radius: 12px; margin-bottom: 12px; background: #fff;
        transition: all .2s;
    }
    .tp-his-item:hover { border-color: #90CAF9; box-shadow: 0 4px 12px rgba(30,136,229,.1); }
    .tp-his-icon {
        width: 42px; height: 42px; border-radius: 10px; background: #E3F2FD; color: #1565C0;
        display: flex; align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0;
    }
    .tp-his-main { flex: 1; min-width: 0; }
    .tp-his-title { font-size: 14.5px; font-weight: 700; color: #263238; }
    .tp-his-meta { font-size: 12px; color: #78909C; margin-top: 3px; }
    .tp-his-ops { display: flex; gap: 8px; }
    .tp-mini {
        border: 1px solid #C9E2FA; background: #fff; color: #1565C0; border-radius: 8px;
        padding: 6px 12px; font-size: 12.5px; cursor: pointer; transition: all .18s; font-family: inherit;
    }
    .tp-mini:hover { background: #1E88E5; color: #fff; border-color: #1E88E5; }
    .tp-mini.danger { color: #E53935; border-color: #FFCDD2; }
    .tp-mini.danger:hover { background: #E53935; color: #fff; border-color: #E53935; }

    .tp-empty { text-align: center; padding: 46px 0; color: #90A4AE; }
    .tp-empty i { font-size: 40px; color: #CFE3F5; display: block; margin-bottom: 12px; }
    .tp-empty p { font-size: 13.5px; }

    .tp-toast {
        position: fixed; left: 50%; bottom: 42px; transform: translateX(-50%) translateY(20px);
        background: #1565C0; color: #fff; padding: 11px 22px; border-radius: 24px; font-size: 14px;
        box-shadow: 0 8px 22px rgba(21,101,192,.32); opacity: 0; transition: all .28s; z-index: 3000;
        pointer-events: none;
    }
    .tp-toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

    /* 地图：一个显示框 + 天数切换按钮 */
    .tp-map-card { margin-top: 16px; }
    .tp-map-tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
    .tp-map-tab {
        border: 1px solid #C9E2FA; background: #fff; color: #1565C0; border-radius: 20px;
        padding: 6px 14px; font-size: 13px; cursor: pointer; transition: all .18s; font-family: inherit;
        display: flex; align-items: center; gap: 6px;
    }
    .tp-map-tab:hover { border-color: #1E88E5; }
    .tp-map-tab.on { background: #1E88E5; border-color: #1E88E5; color: #fff; box-shadow: 0 3px 8px rgba(30,136,229,.25); }
    .tp-map-box {
        height: 380px; border: 1px solid #E1EEFB; border-radius: 14px; overflow: hidden;
        background: #FBFDFF; position: relative;
    }
    .tp-map-box .leaflet-container { height: 100%; width: 100%; font-family: inherit; }
    .tp-map-fallback {
        height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center;
        gap: 10px; color: #78909C; font-size: 13px; text-align: center; padding: 0 24px;
    }
    .tp-map-fallback i { font-size: 30px; color: #CFE3F5; }

    /* 提示条 */
    .tp-alert {
        display: flex; align-items: flex-start; gap: 8px; font-size: 12.5px; line-height: 1.6;
        border-radius: 10px; padding: 10px 14px; margin-bottom: 14px;
    }
    .tp-alert.err { background: #FDECEA; color: #C62828; border: 1px solid #FFCDD2; }
    .tp-alert.warn { background: #FFF8E1; color: #8D6E00; border: 1px solid #FFE082; }
    .tp-alert i { margin-top: 2px; }

    @media (max-width: 900px) {
        .tp-grid, .tp-summary, .tp-tips { grid-template-columns: 1fr 1fr; }
    }
    @media (max-width: 620px) {
        .tp-grid, .tp-summary, .tp-tips { grid-template-columns: 1fr; }
        .tp-steps { flex-direction: column; }
    }
    `;


    const INTEREST_TAGS = [
        { key: 'history', name: '历史人文', icon: 'fa-landmark' },
        { key: 'nature', name: '自然风光', icon: 'fa-tree' },
        { key: 'photo', name: '摄影打卡', icon: 'fa-camera' },
        { key: 'food', name: '美食寻味', icon: 'fa-utensils' },
        { key: 'family', name: '亲子休闲', icon: 'fa-child' }
    ];

    // 目的地固定（项目最终面向北京）
    const FIXED_DEST = '北京市';

    const DIET_TAGS = ['不忌口', '清真', '素食', '清淡少辣', '无辣不欢', '海鲜过敏', '不吃牛肉', '低糖低盐'];

    /* ------------------------------ 状态 ------------------------------ */
    const state = {
        view: 'plan',          // plan | result | history
        plan: null,            // 当前展示的行程
        mapDay: 'all',         // 地图当前查看范围：all | 1 | 2 …
        history: [],           // 服务端历史摘要列表
        historyError: '',      // 历史拉取失败的原因
        form: {
            origin: '北京市区',
            date: todayStr(),
            days: 2,
            people: 2,
            pace: 'normal',
            transport: 'bus',
            budgetAmount: 2000,
            interests: ['nature'],
            diet: ['不忌口'],
            extra: ''
        }
    };

    let rootEl = null;
    let hostEl = null;      // 被顶替的 .main-content
    let toastTimer = null;

    /* ------------------------------ 工具 ------------------------------ */
    function todayStr() {
        const d = new Date();
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    }
    function pad(n) { return n < 10 ? '0' + n : String(n); }
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
            ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    function addDays(dateStr, n) {
        const d = new Date(dateStr + 'T00:00:00');
        if (isNaN(d.getTime())) return dateStr;
        d.setDate(d.getDate() + n);
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    }
    function toast(msg) {
        let el = document.getElementById('tpToast');
        if (!el) {
            el = document.createElement('div');
            el.id = 'tpToast';
            el.className = 'tp-toast';
            document.body.appendChild(el);
        }
        el.textContent = msg;
        requestAnimationFrame(() => el.classList.add('show'));
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove('show'), 2000);
    }

    /* --------------------------- 行程生成（走后端） --------------------------- */
    async function requestPlan(form) {
        return request(API_PLAN, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                origin: form.origin,
                date: form.date,
                days: Number(form.days),
                people: Number(form.people),
                pace: form.pace,
                budgetAmount: Number(form.budgetAmount) || 0,
                transport: form.transport,
                interests: form.interests,
                diet: form.diet,
                extra: form.extra,
                polish: true
            })
        });
    }

    async function loadHistory() {
        try {
            const data = await request(API_HISTORY + '?limit=50', { method: 'GET' });
            state.history = data.plans || [];
        } catch (e) {
            state.history = [];
            state.historyError = e.message;
        }
    }

    /* ------------------------------ 渲染 ------------------------------ */
    function render() {
        if (!rootEl) return;
        rootEl.innerHTML =
            '<div class="tp-shell">' +
            stepsHtml() +
            (state.view === 'plan' ? planView() :
                state.view === 'result' ? resultView() : historyView()) +
            '</div>';
        bindEvents();
        mountMap();
    }

    function stepsHtml() {
        const steps = [
            { key: 'plan', no: '1', name: '填写偏好', desc: '时间 / 人数 / 兴趣' },
            { key: 'result', no: '2', name: '行程结果', desc: state.plan ? '每日安排与建议' : '生成后可用' },
            { key: 'history', no: '3', name: '历史记录', desc: state.history.length + ' 条已保存' }
        ];
        return '<div class="tp-steps">' + steps.map(s => {
            const disabled = (s.key === 'result' && !state.plan) ? ' disabled' : '';
            const active = state.view === s.key ? ' active' : '';
            return '<div class="tp-step' + active + disabled + '" data-step="' + s.key + '">' +
                '<div class="tp-step-no">' + s.no + '</div>' +
                '<div><div class="tp-step-name">' + s.name + '</div>' +
                '<div class="tp-step-desc">' + esc(s.desc) + '</div></div>' +
                '</div>';
        }).join('') +
            '<button class="tp-back-pill" id="tpBackBtn"><i class="fas fa-comment-dots"></i> 返回对话</button>' +
            '</div>';
    }

    /* ---- 规划页 ---- */
    function planView() {
        const f = state.form;
        return '' +
            '<div class="tp-card">' +
            '<div class="tp-card-title"><i class="fas fa-sliders-h"></i> 基本信息<span class="tp-badge">目的地：' + esc(FIXED_DEST) + '</span></div>' +
            '<div class="tp-grid tp-grid-4">' +
            field('出发地', '<input class="tp-input" id="tpOrigin" value="' + esc(f.origin) + '" placeholder="如：北京市区">') +
            field('出发日期', '<input class="tp-input" id="tpDate" type="date" value="' + esc(f.date) + '">') +
            field('游玩天数', '<select class="tp-select" id="tpDays">' +
                [1, 2, 3, 4, 5].map(d => '<option value="' + d + '"' + (d === Number(f.days) ? ' selected' : '') + '>' + d + ' 天</option>').join('') + '</select>') +
            field('出行人数', '<input class="tp-input" id="tpPeople" type="number" min="1" max="20" step="1" value="' + esc(f.people) + '">') +
            '</div>' +
            '</div>' +

            '<div class="tp-card">' +
            '<div class="tp-card-title"><i class="fas fa-heart"></i> 偏好设置<span class="tp-badge">可多选，至少选 1 项兴趣</span></div>' +

            '<div class="tp-label" style="margin-bottom:8px;"><i class="fas fa-compass"></i> 兴趣偏好</div>' +
            '<div class="tp-tags" id="tpTags" style="margin-bottom:16px;">' + INTEREST_TAGS.map(t =>
                '<div class="tp-tag' + (f.interests.indexOf(t.key) >= 0 ? ' on' : '') + '" data-key="' + t.key + '">' +
                '<i class="fas ' + t.icon + '"></i> ' + t.name + '</div>').join('') + '</div>' +

            '<div class="tp-label" style="margin-bottom:8px;"><i class="fas fa-utensils"></i> 饮食偏好</div>' +
            '<div class="tp-tags" id="tpDietTags" style="margin-bottom:16px;">' + DIET_TAGS.map(d =>
                '<div class="tp-tag' + (f.diet.indexOf(d) >= 0 ? ' on' : '') + '" data-diet="' + esc(d) + '">' +
                '<i class="fas fa-check"></i> ' + esc(d) + '</div>').join('') + '</div>' +

            '<div class="tp-grid" style="grid-template-columns:1fr 1fr;">' +
            field('游览节奏', segHtml('pace', [['relax', '轻松'], ['normal', '适中'], ['tight', '紧凑']], f.pace)) +
            field('交通方式', segHtml('transport', [['walk', '徒步为主'], ['bus', '公共交通'], ['car', '自驾']], f.transport)) +
            '</div>' +

            '<div class="tp-grid" style="grid-template-columns:1fr 2fr;margin-top:14px;">' +
            field('预算（人均 / 元）', '<div class="tp-input-suffix"><input class="tp-input" id="tpBudget" type="number" min="0" step="100" value="' + esc(f.budgetAmount) + '"></div>') +
            '</div>' +
            '</div>' +

            '<div class="tp-card">' +
            '<div class="tp-card-title"><i class="fas fa-pen-to-square"></i> 额外要求<span class="tp-badge">选填</span></div>' +
            '<textarea class="tp-textarea" id="tpExtra" placeholder="例如：想看一次日出、带老人出行要少走路、第一天下午才到、不吃辣……">' + esc(f.extra) + '</textarea>' +
            '</div>' +

            '<div class="tp-actions">' +
            '<button class="tp-btn tp-btn-primary" id="tpGenerateBtn"><i class="fas fa-magic"></i> 生成我的行程</button>' +
            '<button class="tp-btn tp-btn-ghost" id="tpResetBtn"><i class="fas fa-undo"></i> 恢复默认</button>' +
            '<button class="tp-btn tp-btn-ghost" data-step="history"><i class="fas fa-history"></i> 历史记录（' + state.history.length + '）</button>' +
            '</div>';
    }

    function field(label, inner) {
        return '<div class="tp-field"><div class="tp-label">' + esc(label) + '</div>' + inner + '</div>';
    }
    function segHtml(group, items, current, big) {
        return '<div class="tp-seg' + (big ? ' tp-seg-big' : '') + '" data-group="' + group + '">' +
            items.map(it => '<div class="tp-seg-item' + (it[0] === current ? ' on' : '') + '" data-val="' + it[0] + '">' + it[1] + '</div>').join('') +
            '</div>';
    }

    /* ---- 结果页 ---- */
    function resultView() {
        const p = state.plan;
        if (!p) return '<div class="tp-card"><div class="tp-empty"><i class="fas fa-route"></i><p>还没有生成行程，请先回到规划页填写偏好。</p></div></div>';

        const tags = p.interests.map(k => {
            const t = INTEREST_TAGS.find(x => x.key === k);
            return t ? '<span class="tp-slot-tip" style="margin:0"><i class="fas ' + t.icon + '"></i> ' + t.name + '</span>' : '';
        }).join(' ');

        return '' +
            (p.degraded ? '<div class="tp-alert warn"><i class="fas fa-circle-info"></i><span>' +
                esc((p.warnings && p.warnings[0]) || '部分智能增强未生效，当前为基础推荐版本。') + '</span></div>' : '') +
            '<div class="tp-card">' +
            '<div class="tp-card-title"><i class="fas fa-map-marked-alt"></i> ' + esc(p.dest) + ' · ' + p.days + ' 天行程' +
            '<span class="tp-badge">生成于 ' + esc(p.createdAt) + '</span></div>' +
            '<div style="font-size:13px;color:#607D8B;margin-bottom:14px;">' +
            '<i class="fas fa-location-dot"></i> 出发地：' + esc(p.origin) +
            ' ｜ <i class="fas fa-calendar"></i> ' + esc(p.date) + ' 起' +
            ' ｜ <i class="fas fa-user-group"></i> ' + (p.people || 1) + ' 人' +
            ' ｜ <i class="fas fa-gauge"></i> ' + esc(p.paceName) + '节奏' +
            ' ｜ <i class="fas fa-bus"></i> ' + esc(p.transportName) +
            (p.diet && p.diet.length && p.diet[0] !== '不忌口' ? ' ｜ <i class="fas fa-utensils"></i> ' + esc(p.diet.join(' / ')) : '') +
            '</div>' +
            '<div style="margin-bottom:6px;">' + tags + '</div>' +
            '<div class="tp-summary">' +
            sumItem('景点总数', p.summary.spots, '个') +
            sumItem('行程条目', p.summary.slots, '项') +
            sumItem('人均预算', p.summary.perPerson, '元') +
            sumItem('预算总额', p.summary.budgetTotal, '元') +
            '</div>' +
            '</div>' +

            '<div class="tp-card">' +
            '<div class="tp-card-title"><i class="fas fa-calendar-day"></i> 每日安排</div>' +
            p.planDays.map(dayHtml).join('') +
            '</div>' +

            '<div class="tp-card tp-map-card">' +
            '<div class="tp-card-title"><i class="fas fa-map-location-dot"></i> 行程地图' +
            '<span class="tp-badge">点击右侧按钮切换查看范围</span></div>' +
            '<div class="tp-map-tabs" id="tpMapTabs">' +
            mapTabHtml('all') + p.planDays.map(d => mapTabHtml(String(d.day))).join('') +
            '</div>' +
            '<div class="tp-map-box" id="tpMapBox"></div>' +
            '</div>' +

            '<div class="tp-card">' +
            '<div class="tp-card-title"><i class="fas fa-lightbulb"></i> 实用建议</div>' +
            '<div class="tp-tips">' +
            tipBox('fa-bus', '交通建议', [p.tips.transport]) +
            tipBox('fa-bed', '住宿建议', [p.tips.hotel]) +
            tipBox('fa-person-walking', '节奏提示', [p.tips.pace]) +
            tipBox('fa-triangle-exclamation', '注意事项', p.tips.notes) +
            '</div>' +
            '</div>' +

            '<div class="tp-actions">' +
            '<button class="tp-btn tp-btn-primary" id="tpSendChatBtn"><i class="fas fa-comments"></i> 发送给导游看看</button>' +
            '<button class="tp-btn tp-btn-ghost" data-step="plan"><i class="fas fa-pen"></i> 修改条件重算</button>' +
            '<button class="tp-btn tp-btn-ghost" data-step="history"><i class="fas fa-history"></i> 历史记录</button>' +
            '</div>';
    }

    /** 地图切换按钮：总览 + 每天 */
    function mapTabHtml(key) {
        const me = state.mapDay || 'all';
        const label = key === 'all' ? '总览' : 'Day' + key;
        return '<button class="tp-map-tab' + (me === key ? ' on' : '') + '" data-mapday="' + key + '">' +
            '<i class="fas ' + (key === 'all' ? 'fa-globe-asia' : 'fa-calendar-day') + '"></i> ' + label + '</button>';
    }

    function sumItem(k, v, unit) {
        return '<div class="tp-sum-item"><div class="tp-sum-k">' + k + '</div>' +
            '<div class="tp-sum-v">' + v + '<small>' + unit + '</small></div></div>';
    }
    /* 实时校准标记：全部对旧数据（无这些字段）安全，取不到就渲染空串 */
    function dayWeatherHtml(d) {
        const rt = (state.plan && state.plan.realtime) || {};
        const w = (rt.weather || {})[d.date];
        if (!w) return '';
        const icon = w.grade === 'bad' ? 'fa-cloud-showers-heavy'
            : (w.grade === 'good' ? 'fa-sun' : 'fa-cloud');
        return '<span class="tp-day-weather"><i class="fas ' + icon + '"></i> ' +
            esc(w.day) + ' ' + esc(w.temp || '') + '</span>';
    }
    function transferHtml(s) {
        const t = s.transfer;
        if (!t || !t.minutes) return '';
        return '<div class="tp-slot-transfer"><i class="fas fa-route"></i> 距上一站约 ' +
            t.minutes + ' 分钟' + (t.source === 'amap' ? '' : '（经验估算）') + '</div>';
    }
    function openTimeHtml(s) {
        if (!s.opentime) return '';
        const full = String(s.opentime);
        const short = full.length > 26 ? full.slice(0, 26) + '…' : full;
        return '<span class="tp-slot-open" title="' + esc(full) +
            '"><i class="fas fa-clock"></i> 开放 ' + esc(short) + '</span>';
    }
    function flagHtml(s) {
        if (s.swapped === 'bad_weather') {
            return '<span class="tp-slot-flag warn"><i class="fas fa-cloud-rain"></i>' +
                ' 原景点为室外，已按天气预报换成室内场馆</span>';
        }
        if (s.swapped === 'closed') {
            return '<span class="tp-slot-flag warn"><i class="fas fa-door-closed"></i>' +
                ' 原景点当天不开放，已替换</span>';
        }
        if (s.swapped === 'open_hours') {
            return '<span class="tp-slot-flag warn"><i class="fas fa-clock"></i>' +
                ' 原景点该时段不营业，已替换</span>';
        }
        if (s.open_adjusted) {
            return '<span class="tp-slot-flag"><i class="fas fa-clock"></i>' +
                ' 已按当天开放时间调整</span>';
        }
        if (s.adjusted) {
            return '<span class="tp-slot-flag"><i class="fas fa-route"></i> 已按实时路况顺延</span>';
        }
        return '';
    }
    function dayHtml(d) {
        return '<div class="tp-day">' +
            '<div class="tp-day-head">' +
            '<span class="tp-day-no">Day ' + d.day + '</span>' +
            '<span class="tp-day-title">' + esc(d.title) + '</span>' +
            dayWeatherHtml(d) +
            '<span class="tp-day-meta">' + esc(d.date) + ' ｜ ' + d.slots.length + ' 项安排</span>' +
            '</div><div class="tp-timeline">' +
            d.slots.map(s =>
                '<div class="tp-slot">' +
                '<div class="tp-slot-time">' + esc(s.time) + '</div>' +
                '<div class="tp-slot-body">' +
                '<div class="tp-slot-name">' + esc(s.name) +
                (s.must ? '<span class="tp-slot-flag"><i class="fas fa-thumbtack"></i> 您点名要去</span>' : '') +
                '</div>' +
                '<div class="tp-slot-desc">' + esc(s.desc) + '</div>' +
                transferHtml(s) +
                openTimeHtml(s) +
                (s.tip ? '<span class="tp-slot-tip"><i class="fas fa-circle-info"></i> ' + esc(s.tip) + '</span>' : '') +
                flagHtml(s) +
                '</div></div>').join('') +
            '</div></div>';
    }
    function tipBox(icon, title, items) {
        return '<div class="tp-tip-box"><h5><i class="fas ' + icon + '"></i> ' + title + '</h5>' +
            '<ul>' + items.map(i => '<li>' + esc(i) + '</li>').join('') + '</ul></div>';
    }

    /* ------------------------------ 地图 ------------------------------ */
    let leafletMap = null;

    function mapWaitingText(key) {
        return key === 'all' ? '行程总览地图' : ('Day' + key + ' 地图');
    }

    function mountMap() {
        if (state.view !== 'result' || !state.plan) return;
        const box = document.getElementById('tpMapBox');
        if (!box) return;
        const key = state.mapDay || 'all';
        const planId = state.plan.id;
        box.innerHTML = '<div class="tp-map-fallback"><i class="fas fa-spinner fa-spin"></i>' +
            '<span>正在获取' + esc(mapWaitingText(key)) + '…</span></div>';

        request(API_MAP(planId) + '?day=' + encodeURIComponent(key), { method: 'GET' })
            .then(payload => {
                if (!state.plan || state.plan.id !== planId || String(state.mapDay) !== key) return; // 已切换则丢弃
                drawMap(box, payload, key);
            })
            .catch(e => {
                box.innerHTML = '<div class="tp-map-fallback">' +
                    '<i class="fas fa-triangle-exclamation"></i>' +
                    '<span>' + esc(e.message) + '</span>' +
                    '<a class="tp-mini" target="_blank" rel="noopener" href="https://uri.amap.com/">用网页版高德代替</a></div>';
            });
    }

    function drawMap(box, payload, key) {
        if (typeof L === 'undefined') {
            box.innerHTML = '<div class="tp-map-fallback"><i class="fas fa-plug-circle-xmark"></i>' +
                '<span>地图组件未加载，请检查网络后刷新页面。</span></div>';
            return;
        }
        box.innerHTML = '';
        if (leafletMap) { try { leafletMap.remove(); } catch (e) { /* 忽略 */ } leafletMap = null; }

        const gmap = L.map(box, { zoomControl: true, attributionControl: false });
        L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
            { subdomains: '1234', maxZoom: 18 }).addTo(gmap);
        leafletMap = gmap;

        const pts = [];
        (payload.markers || []).forEach(mk => {
            if (mk.lng == null || mk.lat == null) return;
            const prefix = key === 'all' ? ('D' + mk.day + ' · ') : '';
            L.marker([mk.lat, mk.lng]).addTo(gmap)
                .bindPopup(esc(prefix + mk.name) + (mk.address ? '<br><span style="color:#888">' + esc(mk.address) + '</span>' : ''));
            pts.push([mk.lat, mk.lng]);
        });
        if (payload.bbox && payload.bbox.length === 2) {
            gmap.fitBounds([[payload.bbox[0][1], payload.bbox[0][0]], [payload.bbox[1][1], payload.bbox[1][0]]],
                { padding: [26, 26] });
        } else if (pts.length) {
            gmap.fitBounds(pts, { padding: [26, 26] });
        }
        if (payload.failed && payload.failed.length) {
            const tip = document.createElement('div');
            tip.style.cssText = 'position:absolute;right:10px;bottom:10px;z-index:500;background:rgba(255,255,255,.92);' +
                'border:1px solid #FFE082;color:#8D6E00;font-size:12px;border-radius:8px;padding:6px 10px;max-width:60%;';
            tip.textContent = payload.failed.length + ' 个地点未能定位，已在图上略去';
            box.appendChild(tip);
        }
        setTimeout(() => gmap.invalidateSize(), 200);
    }

    /* ---- 历史页 ---- */
    function historyView() {
        const banner = state.historyError
            ? '<div class="tp-alert err"><i class="fas fa-circle-exclamation"></i><span>' +
            esc(state.historyError) + '</span></div>' : '';
        if (!state.history.length) {
            return banner + '<div class="tp-card"><div class="tp-empty"><i class="fas fa-box-open"></i>' +
                '<p>还没有保存的行程，去规划页生成一份吧。</p></div>' +
                '<div class="tp-actions"><button class="tp-btn tp-btn-primary" data-step="plan"><i class="fas fa-plus"></i> 新建行程</button></div></div>';
        }
        return banner + '<div class="tp-card">' +
            '<div class="tp-card-title"><i class="fas fa-history"></i> 我的行程记录<span class="tp-badge">共 ' + state.history.length + ' 条</span></div>' +
            state.history.map(h =>
                '<div class="tp-his-item">' +
                '<div class="tp-his-icon"><i class="fas fa-map"></i></div>' +
                '<div class="tp-his-main">' +
                '<div class="tp-his-title">' + esc(h.dest) + ' · ' + h.days + ' 天 · ' + (h.people || 1) + ' 人</div>' +
                '<div class="tp-his-meta">' + esc(h.date) + ' 起 ｜ ' + esc(h.paceName) + '节奏 ｜ ' +
                '人均 ' + (h.summary ? h.summary.perPerson : (h.budgetAmount || 0)) + ' 元 ｜ ' +
                esc(h.budgetTierName || '') + '档 ｜ 生成于 ' + esc(h.createdAt) + '</div>' +
                '</div>' +
                '<div class="tp-his-ops">' +
                '<button class="tp-mini" data-view="' + h.id + '"><i class="fas fa-eye"></i> 查看</button>' +
                '<button class="tp-mini" data-load="' + h.id + '"><i class="fas fa-upload"></i> 载入</button>' +
                '<button class="tp-mini danger" data-del="' + h.id + '"><i class="fas fa-trash"></i> 删除</button>' +
                '</div></div>').join('') +
            '<div class="tp-actions">' +
            '<button class="tp-btn tp-btn-ghost" data-step="plan"><i class="fas fa-plus"></i> 新建行程</button>' +
            '<button class="tp-btn tp-btn-ghost" id="tpClearBtn"><i class="fas fa-trash-can"></i> 清空全部</button>' +
            '</div></div>';
    }

    /* ------------------------------ 事件 ------------------------------ */
    function bindEvents() {
        if (!rootEl) return;

        const back = document.getElementById('tpBackBtn');
        if (back) back.addEventListener('click', closeTripPlanner);

        rootEl.querySelectorAll('.tp-step').forEach(el => {
            if (el.classList.contains('disabled')) return;
            el.addEventListener('click', () => {
                const key = el.dataset.step;
                if (key === 'result' && !state.plan) return;
                state.view = key;
                render();
            });
        });

        rootEl.querySelectorAll('[data-step]').forEach(el => {
            if (el.classList.contains('tp-step')) return;
            el.addEventListener('click', () => {
                const key = el.dataset.step;
                if (key === 'result' && !state.plan) return;
                state.view = key;
                render();
                scrollTop();
            });
        });

        // 分段选择
        rootEl.querySelectorAll('.tp-seg').forEach(seg => {
            seg.querySelectorAll('.tp-seg-item').forEach(item => {
                item.addEventListener('click', () => {
                    seg.querySelectorAll('.tp-seg-item').forEach(i => i.classList.remove('on'));
                    item.classList.add('on');
                    state.form[seg.dataset.group] = item.dataset.val;
                });
            });
        });

        // 兴趣标签
        const tagWrap = document.getElementById('tpTags');
        if (tagWrap) {
            tagWrap.querySelectorAll('.tp-tag').forEach(tag => {
                tag.addEventListener('click', () => {
                    const k = tag.dataset.key;
                    const idx = state.form.interests.indexOf(k);
                    if (idx >= 0) {
                        if (state.form.interests.length === 1) { toast('至少保留一个兴趣标签'); return; }
                        state.form.interests.splice(idx, 1);
                        tag.classList.remove('on');
                    } else {
                        state.form.interests.push(k);
                        tag.classList.add('on');
                    }
                });
            });
        }

        // 饮食标签（「不忌口」与其他互斥）
        const dietWrap = document.getElementById('tpDietTags');
        if (dietWrap) {
            dietWrap.querySelectorAll('.tp-tag').forEach(tag => {
                tag.addEventListener('click', () => {
                    const d = tag.dataset.diet;
                    const idx = state.form.diet.indexOf(d);
                    if (d === '不忌口') {
                        state.form.diet = ['不忌口'];
                        dietWrap.querySelectorAll('.tp-tag').forEach(t =>
                            t.classList.toggle('on', t.dataset.diet === '不忌口'));
                        return;
                    }
                    // 选了其他项则先移除「不忌口」
                    const noneIdx = state.form.diet.indexOf('不忌口');
                    if (noneIdx >= 0) {
                        state.form.diet.splice(noneIdx, 1);
                        const noneTag = dietWrap.querySelector('[data-diet="不忌口"]');
                        if (noneTag) noneTag.classList.remove('on');
                    }
                    if (idx >= 0) {
                        state.form.diet.splice(idx, 1);
                        tag.classList.remove('on');
                        if (!state.form.diet.length) {
                            state.form.diet = ['不忌口'];
                            const noneTag = dietWrap.querySelector('[data-diet="不忌口"]');
                            if (noneTag) noneTag.classList.add('on');
                        }
                    } else {
                        state.form.diet.push(d);
                        tag.classList.add('on');
                    }
                });
            });
        }

        // 生成（走后端，失败就如实报错）
        const gen = document.getElementById('tpGenerateBtn');
        if (gen) gen.addEventListener('click', async () => {
            if (gen.disabled) return;
            readForm();
            gen.disabled = true;
            const html = gen.innerHTML;
            gen.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 正在生成行程…';
            clearPlanError();
            try {
                const plan = await requestPlan(state.form);
                state.plan = plan;
                state.mapDay = 'all';
                state.view = 'result';
                await loadHistory();
                render();
                scrollTop();
                toast('行程已生成并保存，共 ' + plan.days + ' 天');
            } catch (e) {
                showPlanError(e.message);
                toast('生成失败：' + e.message);
            } finally {
                gen.disabled = false;
                gen.innerHTML = html;
            }
        });

        // 恢复默认
        const reset = document.getElementById('tpResetBtn');
        if (reset) reset.addEventListener('click', () => {
            state.form = defaultForm();
            render();
        });

        // 地图天数切换
        rootEl.querySelectorAll('[data-mapday]').forEach(btn => {
            btn.addEventListener('click', () => {
                const key = btn.dataset.mapday;
                if (key === String(state.mapDay)) return;
                state.mapDay = key;
                rootEl.querySelectorAll('[data-mapday]').forEach(b =>
                    b.classList.toggle('on', b.dataset.mapday === key));
                mountMap();
            });
        });

        // 发送给导游（写回聊天区）
        const send = document.getElementById('tpSendChatBtn');
        if (send) send.addEventListener('click', sendPlanToChat);

        // 历史：查看（拉详情）
        rootEl.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', async () => {
            const id = b.dataset.view;
            b.disabled = true;
            try {
                const plan = await request('/api/trip/plan/' + encodeURIComponent(id), { method: 'GET' });
                state.plan = plan;
                state.mapDay = 'all';
                state.view = 'result';
                render();
                scrollTop();
            } catch (e) {
                toast('读取失败：' + e.message);
            } finally {
                b.disabled = false;
            }
        }));

        // 历史：载入条件重算
        rootEl.querySelectorAll('[data-load]').forEach(b => b.addEventListener('click', () => {
            const h = state.history.find(x => x.id === b.dataset.load);
            if (!h) return;
            state.form = {
                origin: h.origin || '北京市区', date: h.date || todayStr(),
                days: h.days || 2, people: h.people || 2,
                pace: h.pace || 'normal', transport: h.transport || 'bus',
                budgetAmount: (h.summary ? h.summary.perPerson : h.budgetAmount) || 2000,
                interests: (h.interests || []).slice(),
                diet: (h.diet && h.diet.length) ? h.diet.slice() : ['不忌口'],
                extra: h.extra || ''
            };
            state.view = 'plan';
            render();
            scrollTop();
            toast('已载入该行程的条件，可修改后重算');
        }));

        // 历史：删除
        rootEl.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', async () => {
            const id = b.dataset.del;
            b.disabled = true;
            try {
                await request('/api/trip/plan/' + encodeURIComponent(id), { method: 'DELETE' });
                state.history = state.history.filter(x => x.id !== id);
                if (state.plan && state.plan.id === id) state.plan = null;
                render();
                toast('已删除该记录');
            } catch (e) {
                toast('删除失败：' + e.message);
                b.disabled = false;
            }
        }));

        // 历史：清空
        const clear = document.getElementById('tpClearBtn');
        if (clear) clear.addEventListener('click', async () => {
            if (!state.history.length) return;
            if (!window.confirm('确定清空全部 ' + state.history.length + ' 条行程记录？')) return;
            clear.disabled = true;
            try {
                await request(API_HISTORY, { method: 'DELETE' });
                state.history = [];
                state.plan = null;
                render();
                toast('历史记录已清空');
            } catch (e) {
                toast('清空失败：' + e.message);
                clear.disabled = false;
            }
        });
    }

    /** 规划页的错误提示条（生成失败时贴在页面顶部） */
    function showPlanError(msg) {
        clearPlanError();
        const el = document.createElement('div');
        el.className = 'tp-alert err';
        el.id = 'tpPlanError';
        el.innerHTML = '<i class="fas fa-circle-exclamation"></i><span>' + esc(msg) + '</span>';
        const shell = rootEl && rootEl.querySelector('.tp-shell');
        if (shell) shell.insertBefore(el, shell.firstChild);
    }
    function clearPlanError() {
        const old = document.getElementById('tpPlanError');
        if (old) old.remove();
    }

    function defaultForm() {
        return {
            origin: '北京市区', date: todayStr(), days: 2, people: 2,
            pace: 'normal', transport: 'bus', budgetAmount: 2000,
            interests: ['nature'], diet: ['不忌口'], extra: ''
        };
    }

    function readForm() {
        const g = id => document.getElementById(id);
        if (g('tpOrigin')) state.form.origin = g('tpOrigin').value.trim() || '北京市区';
        if (g('tpDate')) state.form.date = g('tpDate').value || todayStr();
        if (g('tpDays')) state.form.days = parseInt(g('tpDays').value, 10) || 2;
        if (g('tpPeople')) state.form.people = parseInt(g('tpPeople').value, 10) || 1;
        if (g('tpBudget')) state.form.budgetAmount = parseFloat(g('tpBudget').value) || 0;
        if (g('tpExtra')) state.form.extra = g('tpExtra').value;
    }

    function scrollTop() {
        if (rootEl) rootEl.scrollTop = 0;
    }

    /** 把结果页行程摘要写回聊天区（若页面存在 addMessage 则复用） */
    function sendPlanToChat() {
        const p = state.plan;
        if (!p) return;
        let text = '📅 我规划的【' + p.dest + '】' + p.days + ' 天行程（' + p.date + ' 起，' + (p.people || 1) + ' 人，' + p.paceName + '节奏）：\n';
        p.planDays.forEach(d => {
            text += '\nDay' + d.day + '（' + d.date + '）' + '\n';
            d.slots.forEach(s => { text += '  · ' + s.time + ' ' + s.name + '\n'; });
        });
        if (p.extra) text += '\n额外要求：' + p.extra + '\n';
        text += '\n人均预算约 ' + p.summary.perPerson + ' 元（总额 ' + p.summary.budgetTotal + ' 元），请帮我看看安排是否合理？';

        try {
            if (typeof window.addMessage === 'function') {
                closeTripPlanner();
                window.addMessage(text, true);
                toast('已发送到对话');
                return;
            }
        } catch (e) { /* 忽略，走复制兜底 */ }

        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(
                () => toast('已复制行程，可粘贴到对话框'),
                () => toast('复制失败，请手动复制')
            );
        } else {
            toast('当前页面不支持直接发送，请手动复制');
        }
    }

    /* --------------------------- 打开 / 关闭 --------------------------- */
    function ensureStyle() {
        if (document.getElementById(STYLE_ID)) return;
        const st = document.createElement('style');
        st.id = STYLE_ID;
        st.textContent = CSS;
        document.head.appendChild(st);
    }

    function openTripPlanner(view) {
        ensureStyle();
        ensureFontAwesome();

        hostEl = document.querySelector('.main-content');
        if (hostEl) hostEl.style.display = 'none';

        rootEl = document.getElementById(ROOT_ID);
        if (!rootEl) {
            rootEl = document.createElement('div');
            rootEl.id = ROOT_ID;
            rootEl.className = 'tp-root';
            const container = document.querySelector('.container') || document.body;
            container.appendChild(rootEl);
        }
        rootEl.style.display = '';
        state.view = (view === 'history' || view === 'result') ? view : 'plan';
        if (!state.plan && state.view === 'result') state.view = 'plan';
        render();
        scrollTop();
        // 历史条数先渲染再异步补齐，不阻塞打开
        loadHistory().then(() => {
            if (rootEl && rootEl.style.display !== 'none') render();
        });
    }

    function closeTripPlanner() {
        if (rootEl) rootEl.style.display = 'none';
        if (hostEl) { hostEl.style.display = ''; hostEl = null; }
    }

    function ensureFontAwesome() {
        if (document.querySelector('link[href*="font-awesome"]')) return;
        const l = document.createElement('link');
        l.rel = 'stylesheet';
        l.href = 'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css';
        document.head.appendChild(l);
    }

    // 暴露全局入口
    window.TripPlanner = {
        open: openTripPlanner,
        close: closeTripPlanner,
        loadHistory: loadHistory,
        getState: function () { return state; }
    };
    window.openTripPlanner = openTripPlanner;
    window.closeTripPlanner = closeTripPlanner;
})();
