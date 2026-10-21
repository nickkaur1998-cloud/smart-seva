(function () {
    var canvas = document.getElementById("star-canvas");
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext("2d");
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var colors = ["255,255,255", "255,231,161", "184,216,255", "255,214,170"];
    var stars = [], shooting = null, nextShot = 0, w = 0, h = 0, dpr = 1, running = true;

    function resize() {
        dpr = Math.min(window.devicePixelRatio || 1, 2);
        w = window.innerWidth; h = window.innerHeight;
        canvas.width = w * dpr; canvas.height = h * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        var count = Math.max(60, Math.min(240, Math.round(w * h / 7000)));
        stars = [];
        for (var i = 0; i < count; i++) {
            stars.push({
                x: Math.random() * w, y: Math.random() * h,
                r: Math.random() < 0.12 ? 1.6 + Math.random() * 1.1 : 0.5 + Math.random() * 0.9,
                c: colors[Math.floor(Math.random() * colors.length)],
                phase: Math.random() * Math.PI * 2,
                speed: 0.6 + Math.random() * 2.4,
                base: 0.25 + Math.random() * 0.35
            });
        }
    }

    function draw(t) {
        if (!running) return;
        var time = t / 1000;
        ctx.clearRect(0, 0, w, h);
        for (var i = 0; i < stars.length; i++) {
            var s = stars[i];
            var tw = still ? 0.5 : (Math.sin(time * s.speed + s.phase) + 1) / 2;
            var a = s.base + (1 - s.base) * tw * tw;
            ctx.globalAlpha = a;
            ctx.fillStyle = "rgb(" + s.c + ")";
            ctx.beginPath();
            ctx.arc(s.x, s.y, s.r * (0.8 + 0.5 * tw), 0, 6.2832);
            ctx.fill();
            if (s.r > 1.5 && tw > 0.6) {
                // Bright stars get a four-point sparkle at the peak of the twinkle.
                var len = s.r * 5 * tw;
                ctx.globalAlpha = a * 0.7;
                ctx.strokeStyle = "rgb(" + s.c + ")";
                ctx.lineWidth = 0.7;
                ctx.beginPath();
                ctx.moveTo(s.x - len, s.y); ctx.lineTo(s.x + len, s.y);
                ctx.moveTo(s.x, s.y - len); ctx.lineTo(s.x, s.y + len);
                ctx.stroke();
            }
        }
        if (!still) {
            if (!shooting && time > nextShot) {
                shooting = { x: Math.random() * w * 0.8, y: Math.random() * h * 0.4,
                             vx: 7 + Math.random() * 4, vy: 3 + Math.random() * 2, life: 1 };
                nextShot = time + 6 + Math.random() * 10;
            }
            if (shooting) {
                var g = ctx.createLinearGradient(shooting.x, shooting.y,
                    shooting.x - shooting.vx * 9, shooting.y - shooting.vy * 9);
                g.addColorStop(0, "rgba(255,244,214," + shooting.life + ")");
                g.addColorStop(1, "rgba(255,231,161,0)");
                ctx.globalAlpha = 1;
                ctx.strokeStyle = g; ctx.lineWidth = 1.6;
                ctx.beginPath();
                ctx.moveTo(shooting.x, shooting.y);
                ctx.lineTo(shooting.x - shooting.vx * 9, shooting.y - shooting.vy * 9);
                ctx.stroke();
                shooting.x += shooting.vx; shooting.y += shooting.vy; shooting.life -= 0.014;
                if (shooting.life <= 0 || shooting.x > w + 40 || shooting.y > h + 40) shooting = null;
            }
            requestAnimationFrame(draw);
        }
    }

    document.addEventListener("visibilitychange", function () {
        running = !document.hidden;
        if (running && !still) requestAnimationFrame(draw);
    });
    window.addEventListener("resize", function () { resize(); if (still) draw(0); });
    resize();
    requestAnimationFrame(draw);
})();
