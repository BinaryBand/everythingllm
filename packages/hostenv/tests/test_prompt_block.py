"""hostenv.prompt: the block a workspace's prompt carries."""

from hostenv import prompt

TEXT = "You're the assistant. Now: {datetime} UTC."


def test_the_block_carries_its_version_and_the_variable_to_compare_it_with():
    block = prompt.block(TEXT)
    v = prompt.version(TEXT)
    assert v == prompt.version(TEXT + "\n") and len(v) == 8
    assert block.startswith(f'<everythingllm version="{v}">\n{TEXT}\n')
    assert "{everythingllm_version}" in block and "{datetime}" in block
    assert prompt.written_version(block) == v
    assert prompt.written_version("Speak like a pirate.") is None


def test_splice_replaces_only_the_block_and_keeps_the_text_around_it():
    old = prompt.block("An older prompt.")
    mine = f"Before.\n\n{old}\n\nAfter."
    new = prompt.splice(mine, TEXT)
    assert new == f"Before.\n\n{prompt.block(TEXT)}\n\nAfter."
    assert prompt.splice(new, TEXT) == new


def test_splice_starts_an_empty_or_old_style_prompt_with_the_block():
    assert prompt.splice("", TEXT) == prompt.block(TEXT)
    assert prompt.splice(None, TEXT) == prompt.block(TEXT)
    # What deploys set on every workspace before the block: the bare repo prompt.
    assert prompt.splice(f"{TEXT}\n", TEXT) == prompt.block(TEXT)


def test_splice_keeps_a_prompt_without_a_block_under_the_users_own_heading():
    assert prompt.splice("Speak like a pirate.", TEXT) == (
        f"{prompt.block(TEXT)}\n\nYour own instructions:\nSpeak like a pirate."
    )
