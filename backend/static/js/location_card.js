/* static/js/location_card.js
 *  当前位置卡片：分区下拉 → 景点下拉 → 简介 → 折叠地图 → 问导游。
 *  数据来源：GET /api/attractions（data/attractions.json，含高德 GCJ-02 坐标）。
 *  依赖：index.html 中需有 #locAreaSelect / #locSpotSelect / #locBody / #locMapBtn / #locAskBtn / #locMapWrap / #locMap / #locMapNote 元素；
 *        Leaflet（L 全局，加载由 index.html 引入）；
 *        window.sendText（来自主脚本的「问导游」入口）。
 */
(function () {
    'use strict';

    const locState = { all: [], spot: null, map: null, areaLabels: {}, open: false };
    const LOC_AREA_ORDER = ['core', 'west', 'east', 'north', 'outskirt', 'fun'];
    const LOC_NEAR_RADIUS_KM = 2;

    function locEsc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
            ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function locDist(a, b) {
        // 球面距离（km），用于「周边景点」排序；国内范围误差可忽略
        const R = 6371, rad = d => d * Math.PI / 180;
        const dLat = rad(b.lat - a.lat), dLng = rad(b.lng - a.lng);
        const h = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
        return 2 * R * Math.asin(Math.sqrt(h));
    }

    function locAreaLabel(key) {
        const raw = locState.areaLabels[key] || key;
        return String(raw).split('（')[0] || key;
    }

    function fillLocSpotSelect() {
        const area = document.getElementById('locAreaSelect').value;
        const spotSel = document.getElementById('locSpotSelect');
        const list = area ? locState.all.filter(a => a.area === area) : locState.all;
        spotSel.innerHTML = '';
        list.forEach(a => {
            const opt = document.createElement('option');
            opt.value = a.name;
            opt.textContent = a.name + (a.kind === 'food' ? '（餐饮）' : '');
            spotSel.appendChild(opt);
        });
    }

    function selectLocSpot() {
        const name = document.getElementById('locSpotSelect').value;
        locState.spot = locState.all.find(a => a.name === name) || null;
        renderLocCard();
        if (locState.open) drawLocMap();
    }

    function selectLocSpotByName(name) {
        const target = locState.all.find(a => a.name === name);
        if (!target) return;
        const areaSel = document.getElementById('locAreaSelect');
        if (areaSel.value !== (target.area || '')) {
            areaSel.value = target.area || '';
            fillLocSpotSelect();
        }
        const spotSel = document.getElementById('locSpotSelect');
        for (let i = 0; i < spotSel.options.length; i++) {
            if (spotSel.options[i].value === name) { spotSel.selectedIndex = i; break; }
        }
        selectLocSpot();
    }

    function renderLocCard() {
        const body = document.getElementById('locBody');
        const s = locState.spot;
        if (!s) { body.innerHTML = '<div class="loc-empty">未选择景点</div>'; return; }
        const meta = [];
        if (s.duration) meta.push('<i class="fas fa-clock"></i> 建议 ' + s.duration + ' 小时');
        if (s.ticket != null) {
            meta.push('<i class="fas fa-ticket"></i> ' +
                (Number(s.ticket) === 0 ? '免费' : '门票 ' + s.ticket + ' 元'));
        }
        body.innerHTML =
            '<div class="loc-head"><span class="loc-name">' + locEsc(s.name) + '</span>' +
            '<span class="loc-badge">' + locEsc(locAreaLabel(s.area)) + '</span></div>' +
            (s.desc ? '<div class="loc-desc">' + locEsc(s.desc) + '</div>' : '') +
            (s.tip ? '<div class="loc-tip"><i class="fas fa-lightbulb"></i><span>' +
                locEsc(s.tip) + '</span></div>' : '') +
            '<div class="loc-meta">' + meta.map(m => '<span>' + m + '</span>').join('') + '</div>';
    }

    function locNearby(s) {
        const ranked = locState.all
            .filter(a => a.name !== s.name && a.lat != null && a.lng != null)
            .map(a => ({ a: a, d: locDist(s, a) }))
            .sort((x, y) => x.d - y.d);
        const within = ranked.filter(x => x.d <= LOC_NEAR_RADIUS_KM);
        if (within.length >= 3) return within.slice(0, 8);
        const fallback = ranked.slice(0, 5);
        fallback.expanded = true;   // 2km 内不足 3 个，退化为「最近的 5 个」
        return fallback;
    }

    function toggleLocMap() {
        locState.open = !locState.open;
        document.getElementById('locMapWrap').classList.toggle('show', locState.open);
        document.getElementById('locMapBtn').innerHTML = locState.open
            ? '<i class="fas fa-map"></i> 收起地图'
            : '<i class="fas fa-map"></i> 查看地图';
        if (locState.open) drawLocMap();
    }

    function drawLocMap() {
        const el = document.getElementById('locMap');
        const note = document.getElementById('locMapNote');
        const s = locState.spot;
        if (!el || !s) return;
        if (typeof L === 'undefined') {
            el.innerHTML = '<div style="padding:18px;text-align:center;font-size:12px;color:#999">' +
                '地图组件未加载，请检查网络后刷新页面。</div>';
            note.textContent = '';
            return;
        }
        if (locState.map) { try { locState.map.remove(); } catch (e) { /* 忽略 */ } locState.map = null; }
        el.innerHTML = '';
        const gmap = L.map(el, { zoomControl: true, attributionControl: false });
        L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
            { subdomains: '1234', maxZoom: 18 }).addTo(gmap);
        locState.map = gmap;

        L.marker([s.lat, s.lng], {
            icon: L.divIcon({
                className: '',
                html: '<span class="loc-pin current" style="width:16px;height:16px"></span>',
                iconSize: [16, 16], iconAnchor: [8, 8]
            })
        }).addTo(gmap).bindPopup('<b>' + locEsc(s.name) + '</b><br>当前位置');

        const near = locNearby(s);
        const pts = [[s.lat, s.lng]];
        near.forEach(x => {
            pts.push([x.a.lat, x.a.lng]);
            L.marker([x.a.lat, x.a.lng], {
                icon: L.divIcon({
                    className: '',
                    html: '<span class="loc-pin nearby" style="width:11px;height:11px"></span>',
                    iconSize: [11, 11], iconAnchor: [5, 5]
                })
            }).addTo(gmap)
                .bindPopup('<b>' + locEsc(x.a.name) + '</b><br>直线约 ' + x.d.toFixed(1) + ' km' +
                    '<br><span style="color:#888">点击切换当前位置</span>')
                .on('click', function () { selectLocSpotByName(x.a.name); });
        });

        gmap.fitBounds(pts, { padding: [28, 28], maxZoom: 15 });
        setTimeout(function () { gmap.invalidateSize(); }, 200);

        note.textContent = near.length
            ? '蓝点＝当前位置；绿点＝' + (near.expanded ? '最近的 ' + near.length + ' 个景点（2km 内不足 3 个）'
                : LOC_NEAR_RADIUS_KM + 'km 内的 ' + near.length + ' 个景点') + '，点绿点可直接切换'
            : '暂无周边景点数据';
    }

    function askGuideNearby() {
        const s = locState.spot;
        const input = document.getElementById('userInput');
        if (!s || !input) return;
        const near = locNearby(s).slice(0, 3).map(x => x.a.name).join('、');
        input.value = '我现在在' + s.name + (near ? '，附近有' + near : '') +
            '。请给我讲讲这里有什么值得看的亮点，再推荐接下来还能去哪儿。';
        input.focus();
        if (typeof window.sendText === 'function') window.sendText();
    }

    async function initLocationCard() {
        const areaSel = document.getElementById('locAreaSelect');
        const spotSel = document.getElementById('locSpotSelect');
        const body = document.getElementById('locBody');
        try {
            const res = await fetch('/api/attractions');
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();
            locState.all = (data.attractions || []).filter(a => a.lat != null && a.lng != null);
            locState.areaLabels = data.areas || {};
        } catch (e) {
            body.innerHTML = '<div class="loc-empty"><i class="fas fa-triangle-exclamation"></i> 景点加载失败：'
                + locEsc(e.message) + '</div>';
            return;
        }
        if (!locState.all.length) {
            body.innerHTML = '<div class="loc-empty">景点目录为空</div>';
            return;
        }

        // 分区下拉：只列出真正有数据的分区
        const allOpt = document.createElement('option');
        allOpt.value = '';
        allOpt.textContent = '全部分区';
        areaSel.appendChild(allOpt);
        LOC_AREA_ORDER.filter(k => locState.all.some(a => a.area === k)).forEach(k => {
            const opt = document.createElement('option');
            opt.value = k;
            opt.textContent = locAreaLabel(k);
            areaSel.appendChild(opt);
        });

        areaSel.addEventListener('change', function () { fillLocSpotSelect(); selectLocSpot(); });
        spotSel.addEventListener('change', selectLocSpot);
        document.getElementById('locMapBtn').addEventListener('click', toggleLocMap);
        document.getElementById('locAskBtn').addEventListener('click', askGuideNearby);

        // 默认落在核心区的故宫，没有则取第一条
        areaSel.value = locState.all.some(a => a.area === 'core') ? 'core' : '';
        fillLocSpotSelect();
        const prefer = locState.all.find(a => a.name === '故宫博物院');
        if (prefer) {
            for (let i = 0; i < spotSel.options.length; i++) {
                if (spotSel.options[i].value === prefer.name) { spotSel.selectedIndex = i; break; }
            }
        }
        selectLocSpot();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initLocationCard);
    } else {
        initLocationCard();
    }
})();
