/* Replays a real agent session as a typed-out chat, looping.
 *
 * The transcript below is a verbatim reconstruction of a session in which the
 * agent composed the weather skills from two plain-language requests. Each
 * event is one rendered block: a user message, an assistant line, a collapsed
 * "ran N commands" tool group whose sub-steps reveal in sequence, or an output
 * image card.
 *
 * A user event is typed character by character into the composer box at the
 * bottom, "submitted" (the send button flashes), then dropped into the thread
 * as a bubble while the composer clears back to its placeholder. The player
 * scrolls the thread to follow, and restarts after a pause. With
 * prefers-reduced-motion, or without JS, the static final image (in <noscript>)
 * stands in for the animation.
 */
(function () {
  "use strict";

  var EVENTS = [
    { type: "user", text: "Can you fetch the last 3 weeks of IMERG data in Senegal and group it by week?" },
    {
      type: "tools",
      title: "Viewed 3 files, ran a command",
      steps: [
        "Check workspace",
        "Read IMERG fetch skill",
        "Read resolve-region, clip-region and aggregate-temporal skills",
        "Read aggregate-temporal skill",
      ],
    },
    { type: "say", html: "Good, I have everything I need. Let me run the pipeline: resolve Senegal’s bbox → fetch IMERG → clip → aggregate weekly mean rates → convert to totals." },
    {
      type: "tools",
      title: "Ran 5 commands",
      steps: [
        "Resolve Senegal bbox",
        "Fetch last 3 weeks of IMERG globally",
        "Clip to Senegal",
        "Aggregate to weekly mean rates",
        "Convert weekly rates to totals",
      ],
    },
    { type: "say", html: "3 weekly bins as expected. Now let’s plot them." },
    {
      type: "tools",
      title: "Ran 2 commands, viewed a file, read a file",
      steps: [
        "Read plot skill",
        "Get Senegal GeoJSON for mask",
        "Plot weekly IMERG for Senegal",
      ],
    },
    {
      type: "say",
      html: "Here’s the 3-panel weekly breakdown for Senegal. The pipeline resolved to <strong>2026-05-24 → 2026-06-13</strong> (the latest available IMERG window, which runs ~4 days behind realtime), giving three complete 7-day totals — dry early, with rainfall building in the south by week three as the ITCZ advances.",
    },
    {
      type: "image",
      src: "demo_sen_weekly.png",
      label: "imerg_sen_weekly.png",
      alt: "Three-panel weekly IMERG precipitation map for Senegal, May 24 to June 13 2026, masked to the country border.",
    },
    { type: "user", text: "Can you also include rain in The Gambia?" },
    { type: "say", html: "The Gambia is fully enclosed within Senegal, so the existing clip already covers it spatially — I just need to re-plot with an expanded mask that includes both countries." },
    {
      type: "tools",
      title: "Ran 3 commands, read a file",
      steps: [
        "Resolve Gambia bbox and GeoJSON",
        "Merge Senegal and Gambia GeoJSON into one FeatureCollection",
        "Re-plot with combined Senegal+Gambia mask",
      ],
    },
    { type: "say", html: "Done — Gambia now shows up as a cutout within Senegal’s border, with Banjul labeled. Since it sits entirely inside the original clip bbox, no new data fetch was needed — just a merged mask polygon and a re-plot." },
    {
      type: "image",
      src: "demo_sen_gmb_weekly.png",
      label: "imerg_sen_gmb_weekly.png",
      alt: "Three-panel weekly IMERG precipitation map for Senegal and The Gambia, May 24 to June 13 2026, with The Gambia as a cutout within Senegal's border.",
    },
  ];

  var STEP_REVEAL = 260; // stagger between tool sub-steps (ms)
  var TYPE_MIN = 26; // fastest per-character delay when typing a question (ms)
  var TYPE_VAR = 46; // added random jitter per character (ms)
  var SUBMIT_PAUSE = 480; // pause after typing finishes, before submitting (ms)

  // Composer and control elements, resolved once in start().
  var composerText = null;
  var sendBtn = null;
  var replayBtn = null;
  var placeholder = "";

  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html != null) node.innerHTML = html;
    return node;
  }

  function dwell(ev) {
    if (ev.type === "say") {
      var len = (ev.html || "").replace(/<[^>]+>/g, "").length;
      return Math.min(900 + len * 14, 3000);
    }
    if (ev.type === "tools") return 500 + ev.steps.length * STEP_REVEAL + 650;
    if (ev.type === "image") return 2400;
    return 800;
  }

  function follow(thread) {
    // Keep the newest content in view within the fixed-height thread.
    thread.scrollTop = thread.scrollHeight;
  }

  function userBubble(thread, ev) {
    var row = el("div", "dm-row dm-row-user dm-enter");
    var bubble = el("div", "dm-user");
    bubble.appendChild(el("span", "dm-user-text", null)).textContent = ev.text;
    row.appendChild(bubble);
    thread.appendChild(row);
    requestAnimationFrame(function () { row.classList.remove("dm-enter"); });
    follow(thread);
  }

  // Type the question into the composer, flash send, then drop it into the
  // thread and reset the composer. Resolves once the bubble has landed.
  function typeUser(thread, ev) {
    return new Promise(function (resolve) {
      if (!composerText) {
        userBubble(thread, ev);
        setTimeout(resolve, 360);
        return;
      }
      composerText.classList.add("is-typing");
      composerText.textContent = "";
      var text = document.createTextNode("");
      var caret = el("span", "dm-caret");
      composerText.appendChild(text);
      composerText.appendChild(caret);

      var i = 0;
      function tick() {
        if (i >= ev.text.length) {
          setTimeout(submit, SUBMIT_PAUSE);
          return;
        }
        text.nodeValue += ev.text.charAt(i);
        i += 1;
        setTimeout(tick, TYPE_MIN + Math.floor(Math.random() * TYPE_VAR));
      }
      function submit() {
        if (sendBtn) {
          sendBtn.classList.add("is-active");
          setTimeout(function () { sendBtn.classList.remove("is-active"); }, 220);
        }
        userBubble(thread, ev);
        composerText.classList.remove("is-typing");
        composerText.textContent = placeholder;
        setTimeout(resolve, 380);
      }
      tick();
    });
  }

  function renderSay(thread, ev) {
    var row = el("div", "dm-row dm-enter");
    row.appendChild(el("div", "dm-say", ev.html));
    thread.appendChild(row);
    requestAnimationFrame(function () { row.classList.remove("dm-enter"); });
    follow(thread);
  }

  function renderImage(thread, ev) {
    var row = el("div", "dm-row dm-enter");
    var card = el("figure", "dm-image");
    var img = el("img", "dm-image-img");
    img.src = ev.src;
    img.alt = ev.alt;
    img.loading = "lazy";
    img.decoding = "async";
    var cap = el("figcaption", "dm-image-cap");
    cap.appendChild(el("span", "dm-image-icon", "▦"));
    cap.appendChild(el("span", "dm-image-name", null)).textContent = ev.label;
    cap.appendChild(el("span", "dm-image-kind", "PNG"));
    card.appendChild(img);
    card.appendChild(cap);
    row.appendChild(card);
    thread.appendChild(row);
    img.addEventListener("load", function () { follow(thread); });
    requestAnimationFrame(function () { row.classList.remove("dm-enter"); });
    follow(thread);
  }

  function toolsGroup(done) {
    var group = el("div", "dm-tools" + (done ? " dm-done" : " dm-running"));
    var head = el("div", "dm-tools-head");
    head.appendChild(el("span", "dm-spark"));
    head.appendChild(el("span", "dm-tools-title", null));
    group.appendChild(head);
    return group;
  }

  function stepRow(label) {
    var step = el("div", "dm-step");
    step.appendChild(el("span", "dm-check", "✓"));
    step.appendChild(el("span", "dm-step-text", null)).textContent = label;
    return step;
  }

  // Reveal a tool group: header first, then each sub-step in turn, then mark
  // the group complete. Resolves once all steps are shown.
  function renderTools(thread, ev) {
    return new Promise(function (resolve) {
      var row = el("div", "dm-row dm-enter");
      var group = toolsGroup(false);
      group.querySelector(".dm-tools-title").textContent = ev.title;
      var list = el("div", "dm-steps");
      group.appendChild(list);
      row.appendChild(group);
      thread.appendChild(row);
      requestAnimationFrame(function () { row.classList.remove("dm-enter"); });
      follow(thread);

      var i = 0;
      function next() {
        if (i >= ev.steps.length) {
          group.classList.remove("dm-running");
          group.classList.add("dm-done");
          follow(thread);
          resolve();
          return;
        }
        var step = stepRow(ev.steps[i]);
        step.classList.add("dm-enter");
        list.appendChild(step);
        requestAnimationFrame(function () { step.classList.remove("dm-enter"); });
        follow(thread);
        i += 1;
        setTimeout(next, STEP_REVEAL);
      }
      setTimeout(next, 360);
    });
  }

  function renderStaticAll(thread) {
    EVENTS.forEach(function (ev) {
      if (ev.type === "user") userBubble(thread, ev);
      else if (ev.type === "say") renderSay(thread, ev);
      else if (ev.type === "image") renderImage(thread, ev);
      else if (ev.type === "tools") {
        var row = el("div", "dm-row");
        var group = toolsGroup(true);
        group.querySelector(".dm-tools-title").textContent = ev.title;
        var list = el("div", "dm-steps");
        ev.steps.forEach(function (s) { list.appendChild(stepRow(s)); });
        group.appendChild(list);
        row.appendChild(group);
        thread.appendChild(row);
      }
    });
    thread.scrollTop = 0;
  }

  // Playback finished: hand scroll control to the user and offer a replay.
  function finish(thread) {
    thread.classList.add("dm-scrollable");
    follow(thread);
    if (replayBtn) replayBtn.hidden = false;
  }

  function replay(thread) {
    if (replayBtn) replayBtn.hidden = true;
    thread.classList.remove("dm-scrollable");
    thread.textContent = "";
    thread.scrollTop = 0;
    if (composerText) composerText.textContent = placeholder;
    playOnce(thread);
  }

  // Play the transcript through once, then stop on the finished thread.
  function playOnce(thread) {
    var idx = 0;
    function step() {
      if (idx >= EVENTS.length) {
        finish(thread);
        return;
      }
      var ev = EVENTS[idx];
      idx += 1;
      if (ev.type === "user") {
        typeUser(thread, ev).then(step);
        return;
      }
      if (ev.type === "tools") {
        renderTools(thread, ev).then(function () { setTimeout(step, 650); });
        return;
      }
      if (ev.type === "say") renderSay(thread, ev);
      else if (ev.type === "image") renderImage(thread, ev);
      setTimeout(step, dwell(ev));
    }
    step();
  }

  function start() {
    var thread = document.querySelector("[data-demo-thread]");
    if (!thread) return;
    var stage = thread.closest(".demo-stage") || thread;
    composerText = stage.querySelector(".demo-composer-text");
    sendBtn = stage.querySelector(".demo-composer-send");
    if (composerText) placeholder = composerText.textContent;

    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) {
      renderStaticAll(thread);
      return;
    }

    // A Replay control, hidden until the first run finishes.
    var screen = stage.querySelector(".demo-screen");
    if (screen) {
      replayBtn = el("button", "demo-replay", "&#8635; Replay");
      replayBtn.type = "button";
      replayBtn.hidden = true;
      replayBtn.addEventListener("click", function () { replay(thread); });
      screen.appendChild(replayBtn);
    }

    // Autoplay once when the stage scrolls into view.
    var started = false;
    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting && !started) {
            started = true;
            io.disconnect();
            playOnce(thread);
          }
        });
      }, { threshold: 0.25 });
      io.observe(stage);
    } else {
      playOnce(thread);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
