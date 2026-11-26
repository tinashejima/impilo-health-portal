(function () {
  // Set this to your hosted API address (HTTPS) when you have one, e.g. 'https://api.example.org'.
  var HOSTED_API = 'https://entrance-neural-consequence-lonely.trycloudflare.com';
  var h = location.hostname;
  var local = h === '127.0.0.1' || h === 'localhost' || h === '';
  window.IMPILO_API_ORIGIN = local ? 'http://127.0.0.1:8765' : HOSTED_API;
})();
