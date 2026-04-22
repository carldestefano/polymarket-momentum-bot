// This file is overwritten at CDK deploy time with real values.
// Placeholder lets you run the dashboard locally during development.
window.SCANNER_CONFIG = {
  apiUrl: "",
  region: "",
  userPoolId: "",
  userPoolClientId: "",
  cognitoDomain: "",
  redirectUri: "",
  logoutUri: "",
  scannerId: "default",
};
window.SCANNER_API_URL = window.SCANNER_CONFIG.apiUrl;
