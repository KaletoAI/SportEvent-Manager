// SportAbo — gesamtes Seitenverhalten.
// Keine Inline-Skripte oder on…-Attribute in den Templates: die
// Content-Security-Policy (app/main.py) erlaubt nur Skripte aus /static.
(function () {
    'use strict';

    function store(fn) {
        try { return fn(); } catch (e) { return null; }
    }

    // ── Tabs: Panels umschalten, aktiven Tab pro Seite merken ───────────
    document.querySelectorAll('[data-tabs]').forEach(function (bar) {
        var btns = Array.from(bar.querySelectorAll('.tab-btn'));
        var key = 'tab:' + location.pathname;
        function activate(name, save) {
            btns.forEach(function (b) { b.classList.toggle('active', b.dataset.tab === name); });
            document.querySelectorAll('.tab-panel').forEach(function (p) {
                p.classList.toggle('active', p.id === 'tab-' + name);
            });
            if (save) store(function () { sessionStorage.setItem(key, name); });
        }
        btns.forEach(function (b) {
            b.addEventListener('click', function () { activate(b.dataset.tab, true); });
        });
        var saved = store(function () { return sessionStorage.getItem(key); });
        if (saved && btns.some(function (b) { return b.dataset.tab === saved; })) activate(saved, false);
    });

    // ── Theme-Umschalter: Wahl pro Gerät, Icon + theme-color anpassen ──
    var themeBtn = document.getElementById('theme-toggle');
    function applyTheme(t) {
        document.documentElement.dataset.theme = t;
        if (themeBtn) themeBtn.textContent = t === 'dark' ? '☀️' : '🌙';
        document.querySelectorAll('meta[name="theme-color"]').forEach(function (m) {
            m.setAttribute('content', t === 'dark' ? '#0b1220' : '#2563eb');
        });
    }
    applyTheme(document.documentElement.dataset.theme);
    if (themeBtn) {
        themeBtn.addEventListener('click', function () {
            var next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
            store(function () { localStorage.setItem('theme', next); });
            applyTheme(next);
        });
    }
    // Ohne manuelle Wahl der Systemumschaltung folgen (z. B. Nachtmodus)
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
        if (!store(function () { return localStorage.getItem('theme'); })) applyTheme(e.matches ? 'dark' : 'light');
    });

    // ── Antippbare Karten: Klick auf die Karte öffnet data-href ─────────
    document.addEventListener('click', function (e) {
        var card = e.target.closest('[data-href]');
        if (!card) return;
        if (e.target.closest('a, button, form, input, select, textarea, label, details, summary')) return;
        window.location = card.dataset.href;
    });

    // ── Formulare: Rückfrage (data-confirm) + Doppeltipp-Schutz ─────────
    // Der Text steht in einem Attribut und wird nie als Code ausgewertet —
    // Namen mit ' oder HTML können hier nichts ausrichten.
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (form.dataset.submitted) { e.preventDefault(); return; }
        var question = form.dataset.confirm;
        if (question && !window.confirm(question)) { e.preventDefault(); return; }
        form.dataset.submitted = '1';
        // Erst nach dem Absenden sperren, sonst fehlt der Button im Request
        setTimeout(function () {
            form.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
        }, 0);
    });
    // Zurück-Taste (bfcache): Formulare wieder freigeben
    window.addEventListener('pageshow', function () {
        document.querySelectorAll('form[data-submitted]').forEach(function (form) {
            delete form.dataset.submitted;
            form.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
        });
    });

    // ── Link kopieren (data-copy="#id") und Feld beim Antippen markieren ─
    document.querySelectorAll('[data-copy]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var field = document.querySelector(btn.dataset.copy);
            if (!field) return;
            var label = btn.textContent;
            navigator.clipboard.writeText(field.value).then(function () {
                btn.textContent = 'Kopiert ✓';
                setTimeout(function () { btn.textContent = label; }, 2000);
            });
        });
    });
    document.querySelectorAll('[data-select-all]').forEach(function (field) {
        field.addEventListener('click', function () { field.select(); });
    });

    // ── Login-Code: 6 Einzelfelder, Einfügen, automatisches Absenden ───
    var otp = document.getElementById('otp');
    if (otp) {
        var inputs = Array.from(otp.querySelectorAll('.otp-input'));
        var hidden = document.getElementById('otp-code');
        var codeForm = document.getElementById('code-form');
        var sync = function () {
            hidden.value = inputs.map(function (i) { return i.value; }).join('');
            if (hidden.value.length === 6) {
                if (codeForm.requestSubmit) codeForm.requestSubmit(); else codeForm.submit();
            }
        };
        inputs.forEach(function (inp, idx) {
            inp.addEventListener('input', function () {
                inp.value = inp.value.replace(/\D/g, '').slice(-1);
                if (inp.value && idx < 5) inputs[idx + 1].focus();
                sync();
            });
            inp.addEventListener('keydown', function (e) {
                if (e.key === 'Backspace' && !inp.value && idx > 0) inputs[idx - 1].focus();
            });
            inp.addEventListener('paste', function (e) {
                e.preventDefault();
                var digits = (e.clipboardData.getData('text') || '').replace(/\D/g, '').slice(0, 6);
                digits.split('').forEach(function (ch, i) { if (inputs[i]) inputs[i].value = ch; });
                if (digits.length) inputs[Math.min(digits.length - 1, 5)].focus();
                sync();
            });
        });
        inputs[0].focus();
    }
})();
