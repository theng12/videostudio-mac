// Heavy install: adds the PyTorch (MPS) + Diffusers video engine and its
// supporting deps to the existing conda_env. Required for any /api/generate/*
// endpoint to work. Safe to run more than once.
//
// LOCKED STACK: the generated lock now contains both the base server and every
// generation dependency. The verification step checks exact imports, pipeline
// classes, and package integrity before any success notification is shown.
//
// VERIFY-THEN-NOTIFY: after installing we import the key modules. If any is
// missing the import prints a traceback, the `on` matcher breaks the run, and
// the "installed" notification below never fires. The old script fired that
// notification unconditionally — telling users it worked even on total failure.
//
// Restart flow: if the server is running we stop it first so its Python process
// exits and re-imports the freshly installed torch/diffusers (a long-lived
// uvicorn worker keeps the old sys.modules cache and never sees the new packages
// — the classic "ModuleNotFoundError even though pip succeeded"). We then restart
// whichever server this machine actually runs: the launchd service if installed,
// otherwise start.js.
module.exports = {
  requires: {
    bundle: "ai"
  },
  run: [
    {
      when: "{{running('start.js')}}",
      method: "script.stop",
      params: { uri: "start.js" }
    },
    {
      method: "shell.run",
      params: {
        path: "app",
        conda: {
          "path": "{{path.resolve(cwd, 'conda_env')}}"
        },
        message: [
          "uv pip install -r requirements-generation.lock.txt"
        ]
      }
    },
    {
      // Verify the stack actually imports. A failure prints a traceback and the
      // matcher breaks the run before the success notify. `2>&1` merges stderr
      // so the matcher sees import errors.
      method: "shell.run",
      params: {
        path: "app",
        conda: {
          "path": "{{path.resolve(cwd, 'conda_env')}}"
        },
        message: [
          "python -c \"import torch, diffusers, transformers; names=('LTXConditionPipeline','WanPipeline','WanImageToVideoPipeline','HunyuanVideoPipeline','HunyuanVideoImageToVideoPipeline','CogVideoXPipeline','CogVideoXImageToVideoPipeline','CogVideoXVideoToVideoPipeline'); missing=[n for n in names if not hasattr(diffusers,n)]; assert not missing, missing; print('GEN_VERIFY_OK', torch.__version__, diffusers.__version__, transformers.__version__)\" 2>&1",
          "python -m pip check"
        ],
        on: [{ event: "/(ModuleNotFoundError|ImportError|Traceback)/", break: true }]
      }
    },
    {
      // install_service.sh (not restart_service.sh) rewrites the launchd plist to
      // the current on-disk serve script before relaunching — robust to the
      // serve.sh -> <app>-serve.sh rename. Idempotent.
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
      method: "notify",
      params: {
        html: "Generation engine installed & verified. Server restarted — Generate is ready."
      }
    }
  ]
}
