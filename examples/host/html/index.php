<?php
/** Escape text before placing it in the page. */
function escape_text(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

$identity = $_SERVER['REMOTE_USER'] ?? '';
$name = is_string($_POST['name'] ?? null) ? trim($_POST['name']) : '';
header('Content-Type: text/html; charset=utf-8');
?>
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My site</title>
<link rel="stylesheet" href="/style.css">
<main>
  <h1>My site</h1>
  <p>This page is served by PHP over Reticulum.</p>
  <?php if ($identity !== ''): ?>
    <p>Identified as <code><?= escape_text($identity) ?></code>.</p>
  <?php else: ?>
    <p>You are browsing anonymously.</p>
  <?php endif; ?>
  <form method="post" action="/">
    <label for="name">Your name</label>
    <input id="name" name="name" maxlength="100" value="<?= escape_text($name) ?>">
    <button type="submit">Say hello</button>
  </form>
  <?php if ($name !== ''): ?>
    <p>Hello, <?= escape_text($name) ?>.</p>
  <?php endif; ?>
</main>
</html>
