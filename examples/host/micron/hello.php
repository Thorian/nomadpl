<?php
/** Keep submitted text from becoming Micron formatting or links. */
function plain_micron(string $value): string
{
    return str_replace(["`", "\r", "\n"], [' ', ' ', ' '], $value);
}

$identity = $_SERVER['REMOTE_USER'] ?? '';
$name = is_string($_POST['name'] ?? null) ? trim($_POST['name']) : '';
header('Content-Type: text/plain; charset=utf-8');
echo ">Hello\n";
echo $name !== '' ? 'Hello, ' . plain_micron($name) . ".\n" : "No name was submitted.\n";
echo $identity !== '' ? "Identified as " . $identity . ".\n" : "You are browsing anonymously.\n";
echo "\n`[Back`:/page/index.mu]\n";
