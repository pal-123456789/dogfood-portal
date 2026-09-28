# src/embed/views.py
"""T4 embeddable gallery widget: a dependency-free loader script plus a framed, standalone
gallery page an external site can drop onto any origin.

Two public, anonymous GET views, wired top-level by the orchestrator as `/embed.js` and
`/embed/<ext_id>`:

* ``embed_js`` serves a tiny vanilla-JS snippet (no framework). An external page includes it
  via ``<script src="https://<host>/embed.js"></script>``; on load it finds every
  ``<div data-dogfood-gallery="<event_ext_id>">`` host and injects an <iframe> pointing back
  at ``/embed/<event_ext_id>`` on the script's OWN origin, so the widget always talks to the
  site that served it regardless of where it is embedded.

* ``embed_gallery`` is that iframe's document: a minimal standalone page (it does NOT extend
  base.html) listing the event's SUBMITTED projects. It is the only response in the project
  that is deliberately frameable cross-origin. Framing is enabled per-response, never globally:
  ``@xframe_options_exempt`` drops the site-wide ``X-Frame-Options: DENY`` for this response,
  and the view sets ``Content-Security-Policy: frame-ancestors *`` explicitly. Because
  ContentSecurityPolicyMiddleware uses ``response.setdefault``, the value set here wins while
  every other page keeps ``script-src 'self'`` and ``X-Frame-Options: DENY`` untouched.

The page carries no PII: team name plus project title / track / summary only -- never emails
or member names. The listing reuses the gallery's SUBMITTED-only, event-scoped queryset, so
drafts and withdrawn projects never surface.
"""
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.clickjacking import xframe_options_exempt

from events.models import Event
from submissions.models import Submission

# Frameable-widget policy for embed_gallery ONLY. Set explicitly on the view's response so it
# beats ContentSecurityPolicyMiddleware's setdefault; script-src stays 'self' (the page ships no
# inline <script>), frame-ancestors * lets any external site embed the iframe.
_EMBED_CSP = "frame-ancestors *; script-src 'self'"

# The loader script. Static and request-independent: it resolves its own origin client-side from
# the executing <script> tag, so the served bytes work behind any host or proxy. No framework.
_EMBED_JS = """(function () {
  "use strict";
  // Resolve the origin that served THIS script, so the iframe points back at the same site
  // no matter which external origin embedded it.
  var me = document.currentScript;
  var origin = "";
  if (me && me.src) {
    origin = new URL(me.src).origin;
  } else {
    var ss = document.getElementsByTagName("script");
    for (var k = ss.length - 1; k >= 0; k--) {
      if (ss[k].src && ss[k].src.indexOf("/embed.js") !== -1) {
        origin = new URL(ss[k].src).origin;
        break;
      }
    }
  }

  function mount() {
    var hosts = document.querySelectorAll("div[data-dogfood-gallery]");
    for (var i = 0; i < hosts.length; i++) {
      var host = hosts[i];
      if (host.getAttribute("data-dogfood-embedded") === "1") { continue; }
      var extId = host.getAttribute("data-dogfood-gallery");
      if (!extId) { continue; }
      var frame = document.createElement("iframe");
      frame.src = origin + "/embed/" + encodeURIComponent(extId);
      frame.setAttribute("width", "100%");
      frame.setAttribute("height", "600");
      frame.setAttribute("style", "border:0");
      frame.setAttribute("loading", "lazy");
      frame.setAttribute("title", "Project gallery");
      host.setAttribute("data-dogfood-embedded", "1");
      host.appendChild(frame);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
"""


def embed_js(request):
    """Serve the dependency-free loader script. Anonymous; content is static."""
    return HttpResponse(_EMBED_JS, content_type="application/javascript")


@xframe_options_exempt
def embed_gallery(request, ext_id):
    """Framed, standalone widget listing one event's SUBMITTED projects. 404 on unknown event."""
    event = get_object_or_404(Event, ext_id=ext_id)
    submissions = (Submission.objects
                   .filter(event=event, state=Submission.SUBMITTED)  # SUBMITTED-only == withdrawn-excluded
                   .select_related("team", "track")
                   .order_by("id"))
    response = render(request, "embed/gallery.html", {
        "event": event,
        "submissions": submissions,
    })
    # Explicit per-response CSP: wins over the middleware's setdefault, so only this page is
    # frameable cross-origin. Every other page keeps the site-wide script-src 'self' / DENY.
    response["Content-Security-Policy"] = _EMBED_CSP
    return response
