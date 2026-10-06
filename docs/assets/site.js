/* ==========================================================================
   ModSideDetector 文档站脚本 —— 极简、无依赖、无外部请求
   功能：移动端导航抽屉 / 代码块复制 / 回到顶部 / 目录当前项高亮
   ========================================================================== */
(function () {
  "use strict";

  /* ---------- 1. 移动端导航抽屉 ---------- */
  var navToggle = document.querySelector("[data-nav-toggle]");
  var nav = document.querySelector("[data-nav]");
  if (navToggle && nav) {
    navToggle.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    // 点击导航项后自动收起
    nav.addEventListener("click", function (e) {
      if (e.target.closest("a")) {
        nav.classList.remove("is-open");
        navToggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  /* ---------- 2. 代码块复制按钮 ---------- */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    // 兜底：file:// 等场景下的旧方案
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy") ? resolve() : reject();
      } catch (err) {
        reject(err);
      } finally {
        document.body.removeChild(ta);
      }
    });
  }

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-copy-btn]"),
    function (btn) {
      btn.addEventListener("click", function () {
        var block = btn.closest(".codeblock");
        var code = block ? block.querySelector("code") : null;
        if (!code) return;
        copyText(code.textContent).then(
          function () {
            btn.textContent = "已复制";
            btn.classList.add("is-done");
            setTimeout(function () {
              btn.textContent = "复制";
              btn.classList.remove("is-done");
            }, 1600);
          },
          function () {
            btn.textContent = "复制失败";
            setTimeout(function () {
              btn.textContent = "复制";
            }, 1600);
          }
        );
      });
    }
  );

  /* ---------- 3. 回到顶部 ---------- */
  var toTop = document.querySelector("[data-to-top]");
  if (toTop) {
    var onScroll = function () {
      toTop.classList.toggle("is-visible", window.scrollY > 480);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    toTop.addEventListener("click", function () {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  /* ---------- 4. 目录当前项高亮 ---------- */
  var tocLinks = Array.prototype.slice.call(
    document.querySelectorAll(".doc-aside .toc a")
  );
  if (tocLinks.length && "IntersectionObserver" in window) {
    var map = {};
    tocLinks.forEach(function (a) {
      var id = decodeURIComponent(a.getAttribute("href").slice(1));
      if (id) map[id] = a;
    });
    var headings = Array.prototype.filter.call(
      document.querySelectorAll(".doc-content h1, .doc-content h2, .doc-content h3, .doc-content h4"),
      function (h) {
        return h.id && map[h.id];
      }
    );
    if (headings.length) {
      var current = null;
      var observer = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (!entry.isIntersecting) return;
            var link = map[entry.target.id];
            if (!link || link === current) return;
            if (current) current.classList.remove("is-current");
            link.classList.add("is-current");
            current = link;
          });
        },
        { rootMargin: "-84px 0px -70% 0px", threshold: 0 }
      );
      headings.forEach(function (h) {
        observer.observe(h);
      });
    }
  }
})();
