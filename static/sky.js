// Starry sky: three depth layers of stars that drift slowly and twinkle,
// bright stars with light flares, and occasional shooting stars.
(function () {
    var canvas = document.getElementById("star-canvas");
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext("2d");
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var colors = ["255,255,255", "255,231,161", "184,216,255", "255,214,170"];
    var layers = [], shooting = null, nextShot = 0, w = 0, h = 0, dpr = 1, running = true, last = 0;

    function makeStar(depth) {
        var bright = Math.random() < 0.08 * (depth + 1);
        return {
            x: Math.random() * w,
            y: Math.random() * h,
            depth: depth,                                    // 0 = far, 2 = near
            r: bright ? 1.7 + Math.random() * 1.3 : 0.45 + Math.random() * (0.6 + depth * 0.35),
            c: colors[Math.floor(Math.random() * colors.length)],
            phase: Math.random() * Math.PI * 2,
            speed: 0.5 + Math.random() * 2.2,                // twinkle rate
            base: 0.18 + Math.random() * 0.3,
            drift: (0.8 + depth * 0.9) * (0.6 + Math.random() * 0.4), // px per second
            bright: bright
        };
    }

    function resize() {
        dpr = Math.min(window.devicePixelRatio || 1, 2);
        w = window.innerWidth; h = window.innerHeight;
        canvas.width = w * dpr; canvas.height = h * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        var total = Math.max(90, Math.min(300, Math.round(w * h / 5500)));
        layers = [[], [], []];
        for (var i = 0; i < total; i++) {
            var depth = Math.random() < 0.6 ? 0 : Math.random() < 0.6 ? 1 : 2;
            layers[depth].push(makeStar(depth));
        }
    }

    function drawStar(s, t) {
        var tw = still ? 0.6 : (Math.sin(t * s.speed + s.phase) + 1) / 2;
        var a = s.base + (1 - s.base) * tw * tw;
        ctx.globalAlpha = a;
        ctx.fillStyle = "rgb(" + s.c + ")";
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r * (0.8 + 0.45 * tw), 0, 6.2832);
        ctx.fill();
        if (s.bright && tw > 0.55) {
            // Soft halo and a four-point flare at the peak of the twinkle.
            var halo = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, s.r * 6);
            halo.addColorStop(0, "rgba(" + s.c + ",0.35)");
            halo.addColorStop(1, "rgba(" + s.c + ",0)");
            ctx.globalAlpha = a * tw;
            ctx.fillStyle = halo;
            ctx.beginPath();
            ctx.arc(s.x, s.y, s.r * 6, 0, 6.2832);
            ctx.fill();
            var len = s.r * 7 * tw;
            ctx.globalAlpha = a * 0.6;
            ctx.strokeStyle = "rgb(" + s.c + ")";
            ctx.lineWidth = 0.6;
            ctx.beginPath();
            ctx.moveTo(s.x - len, s.y); ctx.lineTo(s.x + len, s.y);
            ctx.moveTo(s.x, s.y - len); ctx.lineTo(s.x, s.y + len);
            ctx.stroke();
        }
    }

    function drawShooting(time, dt) {
        if (!shooting && time > nextShot) {
            shooting = { x: Math.random() * w * 0.8, y: Math.random() * h * 0.35,
                         vx: 9 + Math.random() * 5, vy: 4 + Math.random() * 2.5, life: 1 };
            nextShot = time + 7 + Math.random() * 11;
        }
        if (!shooting) return;
        var tailX = shooting.x - shooting.vx * 11, tailY = shooting.y - shooting.vy * 11;
        var g = ctx.createLinearGradient(shooting.x, shooting.y, tailX, tailY);
        g.addColorStop(0, "rgba(255,246,220," + shooting.life + ")");
        g.addColorStop(1, "rgba(255,231,161,0)");
        ctx.globalAlpha = 1;
        ctx.strokeStyle = g;
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        ctx.moveTo(shooting.x, shooting.y);
        ctx.lineTo(tailX, tailY);
        ctx.stroke();
        shooting.x += shooting.vx * dt * 60;
        shooting.y += shooting.vy * dt * 60;
        shooting.life -= dt * 0.9;
        if (shooting.life <= 0 || shooting.x > w + 40 || shooting.y > h + 40) shooting = null;
    }

    function draw(t) {
        if (!running) return;
        var time = t / 1000;
        var dt = last ? Math.min(time - last, 0.1) : 0;
        last = time;
        ctx.clearRect(0, 0, w, h);
        for (var d = 0; d < layers.length; d++) {
            for (var i = 0; i < layers[d].length; i++) {
                var s = layers[d][i];
                if (!still) {
                    s.y += s.drift * dt;               // slow fall, so the sky never feels frozen
                    if (s.y > h + 4) { s.y = -4; s.x = Math.random() * w; }
                }
                drawStar(s, time);
            }
        }
        if (!still) drawShooting(time, dt);
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
