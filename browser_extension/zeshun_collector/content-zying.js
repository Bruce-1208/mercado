"use strict";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "PING_ZYING_PAGE") sendResponse({ok: true, zying: true});
  if (message?.type === "READ_ZYING_CONTEXT") {
    sendResponse({ok: true, zying: true, ...zeshunReadZyingPageContext()});
  }
});
