async function loadReference() {
  try {
    const response = await fetch('openapi.json');
    if (!response.ok) throw new Error('Specification unavailable');
    const spec = await response.json();
    if (typeof SwaggerUIBundle !== 'function') throw new Error('Viewer unavailable');
    SwaggerUIBundle({
      spec,
      dom_id: '#swagger-ui',
      deepLinking: true,
      docExpansion: 'list',
      defaultModelsExpandDepth: -1,
      displayRequestDuration: false,
      supportedSubmitMethods: [],
      tryItOutEnabled: false,
      validatorUrl: null,
      persistAuthorization: false,
      presets: [SwaggerUIBundle.presets.apis],
      plugins: [() => ({
        wrapComponents: {
          authorizeBtn: () => () => null,
          authorizationPopup: () => () => null,
        },
      })],
    });
  } catch {
    document.getElementById('swagger-ui').replaceChildren();
    document.getElementById('reference-error').hidden = false;
  }
}
loadReference();
