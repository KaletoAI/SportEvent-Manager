// Theme vor dem ersten Rendern setzen (kein Aufblitzen): gespeicherte
// Wahl pro Gerät, sonst Systemvorgabe. Synchron im <head> geladen.
(function () {
    var t = null;
    try { t = localStorage.getItem('theme'); } catch (e) { /* privat */ }
    if (t !== 'dark' && t !== 'light') {
        t = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    document.documentElement.dataset.theme = t;
})();
