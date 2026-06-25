// Heavy install: adds the PyTorch (MPS) + Diffusers video engine and its
// supporting deps to the existing conda_env.
// Required for any /api/generate/* endpoint to work. Safe to run more than once.
//
// Restart flow: if start.js is running when this script fires, we stop
// it first so its Python process exits, then run the install, then start
// it back up. Without the stop+start the long-lived uvicorn worker keeps
// the old sys.modules cache and never sees the freshly installed torch/diffusers —
// the UI then surfaces "ModuleNotFoundError: No module named 'diffusers'"
// even though pip succeeded. Auto-restarting removes the manual
// "Stop → Start" step users used to have to remember.
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
          "uv pip install -r requirements-generation.txt"
        ]
      }
    },
    {
      method: "script.start",
      params: { uri: "start.js" }
    },
    {
      method: "notify",
      params: {
        html: "Generation engine installed. Server restarted — Generate is ready."
      }
    }
  ]
}
