<!-- источник: шаблон SvelteKit sverdle в oraios/serena (d0f7f926), test/resources/repos/svelte/test_repo/src/routes/(sverdle)/+page.svelte, строки 1–180 и «</style>» -->
<script lang="ts">
	import { enhance } from '$app/forms';
	import { resolve } from '$app/paths';
	import { words } from '$lib/components/Words.svelte';
	import { GAME_VERSION } from '$lib/game';

	let { data } = $props();

	let shake = $state(false);

	let won = $derived(data.answers.at(-1) === 'xxxxx');

	let i = $derived(won ? -1 : data.answers.length);

	let currentGuess = $derived(data.guesses[i] || '');

	let submittable = $derived(currentGuess.length === 5);

	const { classnames, description } = $derived.by(() => {
		let classnames: Record<string, 'exact' | 'close' | 'missing'> = {};
		let description: Record<string, string> = {};
		data.answers.forEach((answer, i) => {
			const guess = data.guesses[i];
			for (let i = 0; i < 5; i += 1) {
				const letter = guess[i];
				if (answer[i] === 'x') {
					classnames[letter] = 'exact';
					description[letter] = 'correct';
				} else if (!classnames[letter]) {
					classnames[letter] = answer[i] === 'c' ? 'close' : 'missing';
					description[letter] = answer[i] === 'c' ? 'present' : 'absent';
				}
			}
		});
		return { classnames, description };
	});

	function update(event: MouseEvent) {
		event.preventDefault();
		const key = (event.target as HTMLButtonElement).getAttribute(
			'data-key'
		);

		if (key === 'backspace') {
			currentGuess = currentGuess.slice(0, -1);
			shake = false;
		} else if (currentGuess.length < 5) {
			currentGuess += key;
		}
	}

	function keydown(event: KeyboardEvent) {
		if (event.metaKey) return;

		if (event.key === 'Enter' && !submittable) return;

		document
			.querySelector(`[data-key="${event.key}" i]`)
			?.dispatchEvent(new MouseEvent('click', { cancelable: true, bubbles: true }));
	}

	console.log(GAME_VERSION, words);
</script>

<svelte:window onkeydown={keydown} />

<svelte:head>
	<title>Sverdle</title>
	<meta name="description" content="A Wordle clone written in SvelteKit" />
</svelte:head>

<h1 class="visually-hidden">Sverdle</h1>

<form
	method="post"
	action="?/enter"
	use:enhance={() => {
		// prevent default callback from resetting the form
		return ({ result, update }) => {
			shake = result.type === 'failure';
			update({ reset: false });
		};
	}}
>
	<a class="how-to-play" href={resolve('/(sverdle)/how-to-play')}>How to play</a>

	<div class="grid" class:playing={!won} class:shake onanimationend={() => (shake = false)}>
		{#each Array.from(Array(6).keys()) as row (row)}
			{@const current = row === i}
			<h2 class="visually-hidden">Row {row + 1}</h2>
			<div class="row" class:current>
				{#each Array.from(Array(5).keys()) as column (column)}
					{@const guess = current ? currentGuess : data.guesses[row]}
					{@const answer = data.answers[row]?.[column]}
					{@const value = guess?.[column] ?? ''}
					{@const selected = current && column === guess.length}
					{@const exact = answer === 'x'}
					{@const close = answer === 'c'}
					{@const missing = answer === '_'}
					<div class="letter" class:exact class:close class:missing class:selected>
						{value}
						<span class="visually-hidden">
							{#if exact}
								(correct)
							{:else if close}
								(present)
							{:else if missing}
								(absent)
							{:else}
								empty
							{/if}
						</span>
						<input name="guess" disabled={!current} type="hidden" {value} />
					</div>
				{/each}
			</div>
		{/each}
	</div>

	<div class="controls">
		{#if won || data.answers.length >= 6}
			{#if !won && data.answer}
				<p>the answer was "{data.answer}"</p>
			{/if}
			<button data-key="enter" class="restart selected" formaction="?/restart">
				{won ? 'you won :)' : `game over :(`} play again?
			</button>
		{:else}
			<div class="keyboard">
				<button data-key="enter" class:selected={submittable} disabled={!submittable}>enter</button>

				<button
					onclick={update}
					data-key="backspace"
					formaction="?/update"
					name="key"
					value="backspace"
				>
					back
				</button>

				{#each ['qwertyuiop', 'asdfghjkl', 'zxcvbnm'] as row (row)}
					<div class="row">
						{#each row as letter, index (index)}
							<button
								onclick={update}
								data-key={letter}
								class={classnames[letter]}
								disabled={submittable}
								formaction="?/update"
								name="key"
								value={letter}
								aria-label="{letter} {description[letter] || ''}"
							>
								{letter}
							</button>
						{/each}
					</div>
				{/each}
			</div>
		{/if}
	</div>
</form>

<style>
	form {
		width: 100%;
		height: 100%;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 1rem;
		flex: 1;
	}

	.how-to-play {
		color: var(--color-text);
	}

</style>
