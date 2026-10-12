/* The time picker, and local-time rendering.
 *
 * Two jobs that share one fact: the viewer's UTC offset.
 *
 * 1. The picker. `datetime-local` gives a wall-clock reading with no zone,
 *    which is ambiguous on its own. The hidden fields carry that reading
 *    plus the offset, so the server can turn it into an instant and ALSO
 *    knows where to cut day boundaries for the charts.
 *
 * 2. Display. Timestamps are rendered server-side as UTC inside <time> so
 *    the page is readable before this runs and if it never runs. Here they
 *    are rewritten into the viewer's zone, unless they have chosen UTC.
 */
(function () {
  "use strict";

  var KEY = "opstrail-tz";
  var MONO = "YYYY-MM-DD HH:mm";

  function wantsUTC() {
    try { return localStorage.getItem(KEY) === "utc"; } catch (e) { return false; }
  }

  function offsetMinutes() {
    // getTimezoneOffset is minutes BEHIND UTC, i.e. the opposite sign of
    // every other representation. Flip it once, here.
    return wantsUTC() ? 0 : -new Date().getTimezoneOffset();
  }

  function zoneLabel() {
    if (wantsUTC()) return "UTC";
    var m = offsetMinutes();
    var sign = m < 0 ? "-" : "+";
    m = Math.abs(m);
    return "UTC" + sign + String(Math.floor(m / 60)).padStart(2, "0") +
           ":" + String(m % 60).padStart(2, "0");
  }

  function pad(n) { return String(n).padStart(2, "0"); }

  function shifted(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return null;
    return wantsUTC()
      ? new Date(d.getTime() + d.getTimezoneOffset() * 60000)
      : d;
  }

  /* ---------------------------------------------------------------- display */

  function renderTimes(root) {
    (root || document).querySelectorAll("time[datetime]").forEach(function (el) {
      var d = shifted(el.getAttribute("datetime"));
      if (!d) return;
      var withSeconds = el.dataset.seconds === "1";
      var text = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
                 " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) +
                 (withSeconds ? ":" + pad(d.getSeconds()) : "");
      if (el.dataset.dateonly === "1") {
        text = text.slice(0, 10);
      } else {
        text += " " + zoneLabel();
      }
      el.textContent = text;
    });
  }

  /* ----------------------------------------------------------------- picker */

  function localValue(iso) {
    var d = shifted(iso);
    if (!d) return "";
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
           "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function wirePicker(box) {
    if (box.dataset.trWired === "1") return;
    box.dataset.trWired = "1";

    var days = box.querySelector("[data-tr-days]");
    var fields = box.querySelector("[data-tr-fields]");
    var from = box.querySelector("[data-tr-from]");
    var to = box.querySelector("[data-tr-to]");
    var hFrom = box.querySelector("[data-tr-hidden-from]");
    var hTo = box.querySelector("[data-tr-hidden-to]");
    var hTz = box.querySelector("[data-tr-hidden-tz]");
    var zone = box.querySelector("[data-tr-zone]");
    if (!days) return;

    if (zone) zone.textContent = zoneLabel();
    // Seed the visible inputs from whatever the server echoed back.
    if (hFrom && hFrom.value) from.value = localValue(hFrom.value);
    if (hTo && hTo.value) to.value = localValue(hTo.value);

    function sync() {
      var absolute = days.value === "abs";
      fields.hidden = !absolute;
      if (!absolute) {
        hFrom.value = hTo.value = hTz.value = "";
        return;
      }
      // Send the wall-clock reading and the offset separately. Converting
      // here and sending only an instant would leave the server guessing
      // where to put the day boundaries on the charts.
      hFrom.value = from.value || "";
      hTo.value = to.value || "";
      hTz.value = String(offsetMinutes());
      // `days` is what the relative path reads; an absolute range must not
      // also submit a stale "Last 30d".
      days.setAttribute("data-tr-was", days.value);
    }

    days.addEventListener("change", sync);
    [from, to].forEach(function (i) { i.addEventListener("change", sync); });

    var form = box.closest("form");
    if (form) {
      form.addEventListener("submit", sync);
      // HTMX serialises on its own trigger, before any submit event.
      form.addEventListener("htmx:configRequest", function (evt) {
        sync();
        if (days.value === "abs") delete evt.detail.parameters.days;
      });
    }
    sync();
  }

  function boot(root) {
    renderTimes(root);
    (root || document).querySelectorAll("[data-timerange]").forEach(wirePicker);
  }

  document.addEventListener("DOMContentLoaded", function () { boot(document); });
  /* An outerHTML swap replaces the nodes these listeners were bound to, so a
     load-time pass alone stops working after the first filter change. */
  document.body.addEventListener("htmx:afterSwap", function (evt) {
    boot(evt.target);
  });

  window.opstrailTimezone = {
    toggle: function () {
      try {
        localStorage.setItem(KEY, wantsUTC() ? "local" : "utc");
      } catch (e) { /* private mode: this session only */ }
      boot(document);
      document.dispatchEvent(new CustomEvent("opstrail:tzchange"));
    },
    label: zoneLabel,
    offsetMinutes: offsetMinutes,
  };
})();
