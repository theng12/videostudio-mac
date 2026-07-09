module.exports = {
  requires: {
    bundle: "ai"
  },
  run: [
    {
      method: "shell.run",
      params: {
        path: "app",
        conda: {
          "path": "{{path.resolve(cwd, 'conda_env')}}",
          "python": "python=3.12"
        },
        message: [
          "python -m pip install --upgrade pip",
          // Install from the fully-pinned lock so a fresh machine gets the exact
          // verified package set (see the lock's header for the upgrade flow).
          "uv pip install -r requirements.lock.txt"
        ]
      }
    }
  ]
}
