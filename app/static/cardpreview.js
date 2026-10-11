/* Render the preview JSON as an Adaptive Card.
 *
 * The server does the templating; this only draws what the server produced.
 * Rendering the template in the browser would make the preview a preview of
 * something other than what gets sent.
 *
 * It is an approximation on purpose: Teams applies its own host config,
 * resolves <at> mentions and sizes the card to the channel. The page says so
 * next to the output, and the raw JSON stays on screen either way.
 */
(function () {
  "use strict";

  // Roughly Teams' light theme. Not the real host config -- Microsoft does
  // not ship one in the npm package -- but closer than the library default,
  // whose accent colours and spacing look nothing like a Teams channel.
  var HOST_CONFIG = {
    spacing: { small: 4, default: 8, medium: 16, large: 24, extraLarge: 32, padding: 16 },
    separator: { lineThickness: 1, lineColor: "#E1DFDD" },
    fontFamily: "'Segoe UI', system-ui, sans-serif",
    fontSizes: { small: 12, default: 14, medium: 16, large: 20, extraLarge: 24 },
    fontWeights: { lighter: 200, default: 400, bolder: 600 },
    containerStyles: {
      default: {
        backgroundColor: "#FFFFFF",
        foregroundColors: {
          default: { default: "#252424", subtle: "#605E5C" },
          accent: { default: "#5B5FC7", subtle: "#7F85F5" },
          attention: { default: "#C4314B", subtle: "#D74654" },
          good: { default: "#13A10E", subtle: "#359B35" },
          warning: { default: "#F2B01A", subtle: "#F8D22A" }
        }
      }
    },
    actions: {
      actionsOrientation: "Horizontal",
      actionAlignment: "Left",
      buttonSpacing: 8,
      spacing: "Default"
    }
  };

  function draw(root) {
    var host = root.querySelector("#cardHost");
    var data = root.querySelector("#cardJson");
    var problem = root.querySelector("#cardProblem");
    if (!host || !data) return;

    function fail(message) {
      host.hidden = true;
      if (problem) {
        problem.hidden = false;
        problem.textContent = message;
      }
    }

    if (typeof AdaptiveCards === "undefined") {
      // Vendored, so this means the file is missing or blocked rather than a
      // network problem. Say which, and keep the JSON visible.
      return fail(
        "Không nạp được thư viện Adaptive Cards từ /static/vendor/ — " +
        "chỉ xem được JSON bên dưới."
      );
    }

    var parsed;
    try {
      parsed = JSON.parse(data.textContent);
    } catch (e) {
      return fail("JSON không hợp lệ: " + e.message);
    }

    try {
      var card = new AdaptiveCards.AdaptiveCard();
      card.hostConfig = new AdaptiveCards.HostConfig(HOST_CONFIG);
      // Buttons are inert here; opening a runbook from a preview would be a
      // surprise, and Action.Submit has nowhere to go.
      card.onExecuteAction = function () {};
      card.parse(parsed);
      var rendered = card.render();
      host.replaceChildren(rendered);
      host.hidden = false;
      if (problem) problem.hidden = true;
    } catch (e) {
      return fail(
        "Thư viện không dựng được card này: " + e.message +
        " — Teams có thể vẫn hiển thị được, hãy đối chiếu JSON."
      );
    }
  }

  function boot(root) { draw(root || document); }

  document.addEventListener("DOMContentLoaded", function () { boot(document); });

  /* hx-swap="outerHTML" replaces the whole preview pane, so a listener bound
     once at load would stop firing after the first keystroke. Delegate from
     body and redraw whatever came back. */
  document.body.addEventListener("htmx:afterSwap", function (evt) {
    if (evt.target && evt.target.id === "tplPreview") boot(evt.target);
  });
})();
