// One-click Update — correct in EVERY run mode (launchd service, start.js, or
// stopped). Replaces the old split of "Update" vs "Update & Restart": one button
// pulls the latest code, refreshes base AND generation deps, and restarts
// whichever server this machine actually runs.
//
// Why this exists: the old flow made users hunt several buttons and often left
// production broken:
//   • "Update" only refreshed BASE deps — a release that bumped an ML dep still
//     needed a separate "Reinstall Generation" click. Now generation deps refresh
//     in the SAME click (when generation is installed).
//   • "Update & Restart" was hardwired to stop/start start.js, but in service
//     mode the server IS the launchd service — so it stopped nothing and then
//     started a SECOND server that fought the service for the fixed port. Now the
//     restart is service-aware and mutually exclusive: kickstart the service, OR
//     start start.js — never both.
//
// Both dependency phases use synchronized exact locks. Import/class checks and
// pip's integrity check gate the success notification.
module.exports = {
  run: [
    {
      // start.js mode: stop it so its Python exits and re-imports after install.
      // Service mode: start.js isn't running (skips) — the service keeps serving
      // through pull+install and only blips at the final kickstart. Stopped: no-op.
      when: "{{running('start.js')}}",
      method: "script.stop",
      params: { uri: "{{path.resolve(cwd, 'start.js')}}" }
    },
    {
      method: "shell.run",
      params: { message: "git pull" }
    },
    {
      // Base deps (always).
      when: "{{exists('conda_env')}}",
      method: "shell.run",
      params: {
        path: "app",
        conda: { "path": "{{path.resolve(cwd, 'conda_env')}}" },
        message: [
          "python -m pip install --upgrade pip",
          "uv pip install -r requirements.lock.txt"
        ]
      }
    },
    {
      // Generation deps — ONLY if generation is installed here (diffusers marker).
      // This is what makes ML-dep bumps land on the same Update click.
      when: "{{exists('conda_env/lib/python3.12/site-packages/diffusers')}}",
      method: "shell.run",
      params: {
        path: "app",
        conda: { "path": "{{path.resolve(cwd, 'conda_env')}}" },
        message: [
          "uv pip install -r requirements-generation.lock.txt"
        ]
      }
    },
    {
      // Restart the REAL server for this machine's mode — mutually exclusive so a
      // second server never fights the service for the fixed port. Use
      // install_service.sh (NOT restart_service.sh): it REWRITES the launchd plist
      // to match the current on-disk scripts before relaunching, so a git pull that
      // renamed the serve script (serve.sh -> <app>-serve.sh) can't leave the plist
      // kickstarting a deleted path. Idempotent + safe to run every update.
      when: "{{exists('service/.installed')}}",
      method: "shell.run",
      params: { message: [ "bash install_service.sh" ] }
    },
    {
      when: "{{!exists('service/.installed')}}",
      method: "script.start",
      params: { uri: "start.js" }
    },
    {
      // Verify generation still imports (if installed). A failure breaks the run
      // here → the success notify is withheld and the terminal shows the error.
      when: "{{exists('conda_env/lib/python3.12/site-packages/diffusers')}}",
      method: "shell.run",
      params: {
        path: "app",
        conda: { "path": "{{path.resolve(cwd, 'conda_env')}}" },
        message: [
          "python -c \"import torch, diffusers, transformers; names=('LTXConditionPipeline','WanPipeline','WanImageToVideoPipeline','HunyuanVideoPipeline','HunyuanVideoImageToVideoPipeline','CogVideoXPipeline','CogVideoXImageToVideoPipeline','CogVideoXVideoToVideoPipeline'); missing=[n for n in names if not hasattr(diffusers,n)]; assert not missing, missing; print('GEN_VERIFY_OK')\" 2>&1",
          "python -m pip check"
        ],
        on: [{ event: "/(ModuleNotFoundError|ImportError|Traceback)/", break: true }]
      }
    },
    {
      method: "notify",
      params: { html: "Updated &amp; restarted — you're on the latest version." }
    }
  ]
}
