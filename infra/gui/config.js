// Overwritten by CDK BucketDeployment at deploy time with real values.
// When served locally (file://) everything is blank and the UI shows
// a configuration warning.
window.BOT_CONFIG = {
  apiUrl: "",
  region: "",
  userPoolId: "",
  userPoolClientId: "",
  cognitoDomain: "",
  redirectUri: "",
  logoutUri: "",
};
window.BOT_API_URL = window.BOT_CONFIG.apiUrl;
