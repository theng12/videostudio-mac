module.exports = {
  run: [
    {
      method: "input",
      params: {
        title: "Reset",
        description: "This will delete the conda_env folder and all installed dependencies. You'll need to reinstall before using the app again.",
        type: "modal",
        form: [{
          type: "checkbox",
          key: "confirmed",
          title: "Yes, delete conda_env",
          description: "Remove all installed Python packages and virtual environment"
        }]
      }
    },
    {
      method: "local.set",
      params: {
        confirmed: "{{input.confirmed}}"
      }
    },
    {
      when: "{{local.confirmed}}",
      method: "fs.rm",
      params: {
        path: "conda_env"
      }
    }
  ]
}
