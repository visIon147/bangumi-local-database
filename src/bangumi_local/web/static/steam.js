"use strict";

// POST forms cannot place the double-submit CSRF value in a request header on
// their own. Keep all Steam mutations as ordinary, accessible forms and add the
// required local-only header through the shared bldFetch helper.
document.addEventListener("submit", async function submitSteamForm(event) {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") {
    return;
  }
  if (!form.action.includes("/steam/")) {
    return;
  }
  event.preventDefault();
  const submitter = event.submitter;
  if (submitter instanceof HTMLButtonElement) {
    submitter.disabled = true;
  }
  try {
    const response = await window.bldFetch(form.action, {
      method: "POST",
      body: new FormData(form),
      redirect: "follow"
    });
    if (response.redirected) {
      window.location.assign(response.url);
      return;
    }
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("text/html")) {
      throw new Error(await response.text());
    }
    const html = await response.text();
    document.open();
    document.write(html);
    document.close();
  } catch (error) {
    window.alert(`Steam 请求失败：${String(error)}`);
    if (submitter instanceof HTMLButtonElement) {
      submitter.disabled = false;
    }
  }
});
