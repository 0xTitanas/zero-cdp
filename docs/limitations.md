# Limitations

BareCDP is intentionally small. It is not a full Playwright or Selenium replacement.

Current limitations include no built-in full locator engine, no browser-context abstraction, no request-routing wrapper, no HAR/video/tracing helpers, and no frame/shadow-DOM convenience layer. Raw CDP calls can reach many of these capabilities, but wrappers are not implemented yet.
