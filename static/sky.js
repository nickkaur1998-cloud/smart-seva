// Ocean backdrop: slowly rising bubbles on a dark turquoise sea.
(function () {
    var canvas = document.getElementById("star-canvas");
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext("2d");
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var bubbles = [], w = 0, h = 0, dpr = 1, running = true, last = 0;

    function spawn(initial) {
        return {
            x: Math.random() * w,
            y: initial ? Math.random() * h : h + 20,
            r: 1 + Math.random() * 4.5,
            speed: 12 + Math.random() * 22,      // px per second
            wobble: Math.random() * Math.PI * 2,
            wobbleSpeed: 0.8 + Math.random() * 1.2,
            alpha: 0.18 + Math.random() * 0.25
        };
    }

    function resize() {
        dpr = Math.min(window.devicePixelRatio || 1, 2);
        w = window.innerWidth; h = window.innerHeight;
        canvas.width = w * dpr; canvas.height = h * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        var count = Math.max(25, Math.min(90, Math.round(w * h / 22000)));
        bubbles = [];
        for (var i = 0; i < count; i++) bubbles.push(spawn(true));
    }

    function draw(t) {
        if (!running) return;
        var dt = last ? Math.min((t - last) / 1000, 0.1) : 0;
        last = t;
        ctx.clearRect(0, 0, w, h);
        for (var i = 0; i < bubbles.length; i++) {
            var b = bubbles[i];
            b.y -= b.speed * dt;
            b.wobble += b.wobbleSpeed * dt;
            var x = b.x + Math.sin(b.wobble) * 6;
            if (b.y < -20) bubbles[i] = spawn(false);
            ctx.globalAlpha = b.alpha;
            ctx.strokeStyle = "rgba(190,250,250,1)";
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.arc(x, b.y, b.r, 0, 6.2832);
            ctx.stroke();
            ctx.globalAlpha = b.alpha * 0.5;
            ctx.fillStyle = "rgba(200,255,255,1)";
            ctx.beginPath();
            ctx.arc(x - b.r * 0.35, b.y - b.r * 0.35, b.r * 0.3, 0, 6.2832);
            ctx.fill();
        }
        ctx.globalAlpha = 1;
        if (!still) requestAnimationFrame(draw);
    }

    document.addEventListener("visibilitychange", function () {
        running = !document.hidden;
        last = 0;
        if (running && !still) requestAnimationFrame(draw);
    });
    window.addEventListener("resize", function () { resize(); if (still) draw(0); });
    resize();
    requestAnimationFrame(draw);
})();
