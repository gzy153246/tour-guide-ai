/* static/js/panda.js
 *  熊猫宠物动画：眨眼、表情切换、说话嘴型、点击反馈。
 *  依赖：index.html 中需有 #pandaPet / #pandaMouth / #pandaEyeLeft / #pandaEyeRight / #pandaBubble 元素。
 *  对外暴露（挂 window，供 index.html 中其他脚本使用）：
 *    - pandaListenToSpeech(isListening)
 *    - setPandaExpression('happy' | 'surprised' | 'talking' | 其他)
 */
(function () {
    'use strict';

    let pandaTalkTimer = null;
    let pandaBlinkTimer = null;
    let isPandaSpeaking = false;

    let pandaPet, pandaMouth, pandaEyeLeft, pandaEyeRight, pandaBubble;

    function startBlinkCycle() {
        if (pandaBlinkTimer) clearInterval(pandaBlinkTimer);
        pandaBlinkTimer = setInterval(() => {
            if (isPandaSpeaking) return;
            blinkPanda();
        }, 2800 + Math.random() * 1800);
    }

    function blinkPanda() {
        pandaEyeLeft.classList.add('closed');
        pandaEyeRight.classList.add('closed');
        setTimeout(() => {
            pandaEyeLeft.classList.remove('closed');
            pandaEyeRight.classList.remove('closed');
        }, 150);
    }

    function setPandaExpression(expr) {
        pandaEyeLeft.classList.remove('happy', 'surprised', 'closed');
        pandaEyeRight.classList.remove('happy', 'surprised', 'closed');
        pandaMouth.classList.remove('happy', 'talking', 'surprised');
        if (expr === 'happy') {
            pandaEyeLeft.classList.add('happy');
            pandaEyeRight.classList.add('happy');
            pandaMouth.classList.add('happy');
        } else if (expr === 'surprised') {
            pandaEyeLeft.classList.add('surprised');
            pandaEyeRight.classList.add('surprised');
            pandaMouth.classList.add('surprised');
        } else if (expr === 'talking') {
            pandaMouth.classList.add('talking');
            pandaEyeLeft.classList.remove('happy', 'surprised', 'closed');
            pandaEyeRight.classList.remove('happy', 'surprised', 'closed');
        } else {
            pandaMouth.classList.remove('happy', 'talking', 'surprised');
            pandaEyeLeft.classList.remove('happy', 'surprised', 'closed');
            pandaEyeRight.classList.remove('happy', 'surprised', 'closed');
        }
    }

    function startPandaSpeaking() {
        if (isPandaSpeaking) return;
        isPandaSpeaking = true;
        pandaPet.classList.add('speaking');
        setPandaExpression('talking');
        pandaBubble.textContent = '🗣 说给你听~';
        let open = false;
        let count = 0;
        if (pandaTalkTimer) clearInterval(pandaTalkTimer);
        pandaTalkTimer = setInterval(() => {
            if (!isPandaSpeaking) {
                clearInterval(pandaTalkTimer);
                pandaTalkTimer = null;
                return;
            }
            open = !open;
            const mouthShape = pandaMouth.querySelector('.mouth-shape');
            if (open) {
                mouthShape.style.transform = 'scaleY(1.3) scaleX(0.9)';
                mouthShape.style.borderBottomWidth = '6px';
            } else {
                mouthShape.style.transform = 'scaleY(0.7) scaleX(1.1)';
                mouthShape.style.borderBottomWidth = '3px';
            }
            count++;
            if (count % 10 === 0) {
                mouthShape.style.transform = 'scaleY(0.5) scaleX(0.8)';
                setTimeout(() => {
                    if (isPandaSpeaking) {
                        mouthShape.style.transform = open ? 'scaleY(1.3) scaleX(0.9)' : 'scaleY(0.7) scaleX(1.1)';
                    }
                }, 120);
            }
        }, 140);
    }

    function stopPandaSpeaking() {
        isPandaSpeaking = false;
        pandaPet.classList.remove('speaking');
        if (pandaTalkTimer) {
            clearInterval(pandaTalkTimer);
            pandaTalkTimer = null;
        }
        const mouthShape = pandaMouth.querySelector('.mouth-shape');
        mouthShape.style.transform = '';
        mouthShape.style.borderBottomWidth = '';
        setPandaExpression('happy');
        pandaBubble.textContent = '💬 听着呢~';
        pandaPet.classList.remove('speaking');
    }

    function pandaListenToSpeech(isListening) {
        if (isListening) startPandaSpeaking();
        else stopPandaSpeaking();
    }

    function initPanda() {
        pandaPet = document.getElementById('pandaPet');
        pandaMouth = document.getElementById('pandaMouth');
        pandaEyeLeft = document.getElementById('pandaEyeLeft');
        pandaEyeRight = document.getElementById('pandaEyeRight');
        pandaBubble = document.getElementById('pandaBubble');
        if (!pandaPet || !pandaMouth || !pandaEyeLeft || !pandaEyeRight || !pandaBubble) return;
        window.pandaBubble = pandaBubble;   // 主脚本会直接改气泡文案

        setPandaExpression('happy');
        startBlinkCycle();
        pandaPet.addEventListener('click', function () {
            if (isPandaSpeaking) {
                pandaBubble.textContent = '😊 嘿！';
                setTimeout(() => { pandaBubble.textContent = isPandaSpeaking ? '🗣 说给你听~' : '💬 听着呢~'; }, 800);
            } else {
                pandaBubble.textContent = '🐼 找我玩呀！';
                setPandaExpression('happy');
                setTimeout(() => { pandaBubble.textContent = '💬 听着呢~'; }, 1000);
            }
        });

        window.addEventListener('beforeunload', function () {
            if (pandaTalkTimer) clearInterval(pandaTalkTimer);
            if (pandaBlinkTimer) clearInterval(pandaBlinkTimer);
        });
    }

    // 暴露给 index.html 内部脚本（按原始同名约定）
    window.pandaListenToSpeech = pandaListenToSpeech;
    window.setPandaExpression = setPandaExpression;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initPanda);
    } else {
        initPanda();
    }
})();
