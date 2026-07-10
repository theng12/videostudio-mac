module.exports = {
  version: "3.6",
  title: "Video Studio KH",
  description: "Apple Silicon video studio — text-to-video & video-to-video, model catalog, download manager.",
  icon: "icon.png",
  menu: async (kernel, info) => {
    const installed = info.exists("conda_env")
    // Generation engine = the heavy PyTorch/Diffusers stack from install_generation.js.
    // `diffusers` in site-packages is the marker that the video engine is installed.
    const generationInstalled = info.exists("conda_env/lib/python3.12/site-packages/diffusers")
    // Always-on launchd service installed? (marker dropped by install_service.sh)
    const serviceInstalled = info.exists("service/.installed")
    const servicePort = 47872
    // Offered in the normal (non-service) menus so the user can convert to a
    // background service. When the service IS installed we return a dedicated
    // "service mode" menu below instead.
    const serviceItem = { icon: "fa-solid fa-heart-pulse", text: "Install as Startup Service", href: "service.js" }
    const running = {
      install: info.running("install.js"),
      install_generation: info.running("install_generation.js"),
      start: info.running("start.js"),
      update: info.running("update.js"),
      updateRestart: info.running("update_and_restart.js"),
      reset: info.running("reset.js")
    }

    if (running.install) {
      return [{
        default: true,
        icon: "fa-solid fa-plug",
        text: "Installing",
        href: "install.js"
      }]
    }
    if (running.install_generation) {
      return [{
        default: true,
        icon: "fa-solid fa-wand-magic-sparkles",
        text: "Installing Generation",
        href: "install_generation.js"
      }]
    }
    if (running.update) {
      return [{
        default: true,
        icon: "fa-solid fa-rotate",
        text: "Updating",
        href: "update.js"
      }]
    }
    if (running.updateRestart) {
      return [{
        default: true,
        icon: "fa-solid fa-rotate",
        text: "Updating & Restarting",
        href: "update_and_restart.js"
      }]
    }
    if (running.reset) {
      return [{
        default: true,
        icon: "fa-solid fa-broom",
        text: "Resetting",
        href: "reset.js"
      }]
    }

    if (!installed) {
      return [{
        default: true,
        icon: "fa-solid fa-plug",
        text: "Install",
        href: "install.js"
      }]
    }

    // ── Service mode ──
    // The launchd service runs the server itself (on the fixed port), so Pinokio
    // doesn't "see" it as running. Show a dedicated menu: open the running UI,
    // check status, restart, view logs, uninstall — and NO "Start" button (that
    // would fight the service for the port). Convert back by uninstalling.
    if (serviceInstalled) {
      const cb = Date.now()
      const svcUrl = `http://localhost:${servicePort}`
      return [
        { default: true, icon: "fa-solid fa-rocket", text: "Open UI (service)", href: `${svcUrl}/?_cb=${cb}` },
        { icon: "fa-solid fa-arrow-up-right-from-square",
          text: `Port ${servicePort} · Open in Browser`,
          href: "open_external.js", params: { url: svcUrl } },
        { icon: "fa-solid fa-stethoscope", text: "Check Service Status", href: "service_status.js" },
        { icon: "fa-solid fa-rotate-right", text: "Restart Service", href: "service_restart.js" },
        { icon: "fa-solid fa-screwdriver-wrench", text: "Repair · take over port", href: "service.js" },
        { icon: "fa-solid fa-folder-open", text: "Service Logs", href: "logs/service?fs=true" },
        { icon: "fa-solid fa-film", text: "Outputs", href: "app/output?fs=true" },
        { icon: "fa-solid fa-folder-tree", text: "HF Cache", href: "cache/HF_HOME/hub?fs=true" },
        { icon: "fa-regular fa-circle-xmark", text: "Uninstall Startup Service", href: "unservice.js" },
        { icon: "fa-solid fa-rotate", text: "Update", href: "update.js" }
      ]
    }

    if (running.start) {
      const local = info.local("start.js")
      if (local && local.url) {
        // Cache-bust so Pinokio's embedded webview can't serve a stale build.
        // menu() re-runs every time the sidebar refreshes, so each click on
        // "Open UI" loads a unique URL the webview hasn't cached.
        const cb = Date.now()
        const bust = `?_cb=${cb}`
        // Browser-friendly URL: replace 0.0.0.0 (server-bind) with localhost
        // (client-reachable) so the external browser can actually connect.
        // Also pluck the port for compact display in the sidebar.
        const browserUrl = local.url.replace("0.0.0.0", "localhost")
        const portMatch = local.url.match(/:(\d+)/)
        const port = portMatch ? portMatch[1] : "?"
        return [
          {
            default: true,
            icon: "fa-solid fa-rocket",
            text: "Open UI",
            href: `${local.url}/${bust}`
          },
          {
            icon: "fa-solid fa-cube",
            text: "Models",
            href: `${local.url}/${bust}#/models`
          },
          {
            icon: "fa-solid fa-download",
            text: "Downloads",
            href: `${local.url}/${bust}#/downloads`
          },
          // ── Escape hatch (v1.1.1) ──
          // If the embedded webview ever caches a broken state and shows a
          // black/blank screen, the user is stranded because Pinokio's
          // refresh buttons hit the same cached webview. These two items
          // make the URL visible + give a one-click way out:
          //   1. The "Port: 47872 (open externally)" item opens the WebUI
          //      in the system default browser via open_external.js.
          //   2. Even without clicking, the port number is always visible in
          //      the sidebar — read it and type into Chrome / Safari if all
          //      else fails.
          {
            icon: "fa-solid fa-arrow-up-right-from-square",
            text: `Port ${port} · Open in Browser`,
            href: "open_external.js",
            params: { url: browserUrl }
          },
          {
            icon: "fa-solid fa-terminal",
            text: "Terminal",
            href: "start.js"
          },
          {
            icon: "fa-solid fa-rotate",
            text: "Update",
            href: "update.js"
          },
          {
            icon: "fa-solid fa-folder-tree",
            text: "HF Cache",
            href: "cache/HF_HOME/hub?fs=true"
          },
          {
            icon: "fa-solid fa-film",
            text: "Outputs",
            href: "app/output?fs=true"
          },
          {
            icon: "fa-solid fa-wand-magic-sparkles",
            text: generationInstalled ? "Reinstall Generation" : "Install Generation",
            href: "install_generation.js"
          },
          serviceItem
        ]
      }
      return [{
        default: true,
        icon: "fa-solid fa-terminal",
        text: "Terminal",
        href: "start.js"
      }]
    }

    return [
      {
        default: true,
        icon: "fa-solid fa-power-off",
        text: "Start",
        href: "start.js"
      },
      {
        icon: "fa-solid fa-folder-tree",
        text: "HF Cache",
        href: "cache/HF_HOME/hub?fs=true"
      },
      {
        icon: "fa-solid fa-film",
        text: "Outputs",
        href: "app/output?fs=true"
      },
      {
        icon: "fa-solid fa-wand-magic-sparkles",
        text: generationInstalled ? "Reinstall Generation" : "Install Generation",
        href: "install_generation.js"
      },
      serviceItem,
      {
        icon: "fa-solid fa-rotate",
        text: "Update",
        href: "update.js"
      },
      {
        icon: "fa-solid fa-plug",
        text: "Reinstall",
        href: "install.js"
      },
      {
        icon: "fa-regular fa-circle-xmark",
        text: "Reset",
        href: "reset.js"
      }
    ]
  }
}
