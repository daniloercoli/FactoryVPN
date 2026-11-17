document.querySelectorAll('[data-copy]').forEach((button) => {
  button.addEventListener('click', async () => {
    const example = document.getElementById(button.dataset.copy);
    const status = document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText(example.textContent);
      button.textContent = 'Copied';
      status.textContent = 'Code copied to clipboard.';
      setTimeout(() => { button.textContent = 'Copy'; }, 2000);
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(example);
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = 'Select and copy the highlighted code using your browser.';
      button.textContent = 'Code selected';
    }
  });
});
