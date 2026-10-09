<?php // источник: WordPress 6.6.2, wp-content/themes/twentytwelve/inc/custom-header.php, строки 62–168

/**
 * Style the header text displayed on the blog.
 *
 * get_header_textcolor() options: 515151 is default, hide text (returns 'blank'), or any hex value.
 *
 * @since Twenty Twelve 1.0
 */
function twentytwelve_header_style() {
	$text_color = get_header_textcolor();

	// If no custom options for text are set, let's bail.
	if ( get_theme_support( 'custom-header', 'default-text-color' ) === $text_color ) {
		return;
	}

	// If we get this far, we have custom styles.
	?>
	<style type="text/css" id="twentytwelve-header-css">
	<?php
		// Has the text been hidden?
	if ( ! display_header_text() ) :
		?>
	.site-title,
	.site-description {
		position: absolute;
		clip: rect(1px 1px 1px 1px); /* IE7 */
		clip: rect(1px, 1px, 1px, 1px);
	}
		<?php
		// If the user has set a custom color for the text, use that.
		else :
			?>
		.site-header h1 a,
		.site-header h2 {
			color: #<?php echo $text_color; ?>;
		}
	<?php endif; ?>
	</style>
	<?php
}

/**
 * Style the header image displayed on the Appearance > Header admin panel.
 *
 * @since Twenty Twelve 1.0
 */
function twentytwelve_admin_header_style() {
	?>
	<style type="text/css" id="twentytwelve-admin-header-css">
	.appearance_page_custom-header #headimg {
		border: none;
		font-family: "Open Sans", Helvetica, Arial, sans-serif;
	}
	#headimg h1,
	#headimg h2 {
		line-height: 1.84615;
		margin: 0;
		padding: 0;
	}
	#headimg h1 {
		font-size: 26px;
	}
	#headimg h1 a {
		color: #515151;
		text-decoration: none;
	}
	#headimg h1 a:hover {
		color: #21759b !important; /* Has to override custom inline style. */
	}
	#headimg h2 {
		color: #757575;
		font-size: 13px;
		margin-bottom: 24px;
	}
	#headimg img {
		max-width: <?php echo get_theme_support( 'custom-header', 'max-width' ); ?>px;
	}
	</style>
	<?php
}

/**
 * Output markup to be displayed on the Appearance > Header admin panel.
 *
 * This callback overrides the default markup displayed there.
 *
 * @since Twenty Twelve 1.0
 */
function twentytwelve_admin_header_image() {
	$style = 'color: #' . get_header_textcolor() . ';';
	if ( ! display_header_text() ) {
		$style = 'display: none;';
	}
	?>
	<div id="headimg">
		<h1 class="displaying-header-text"><a id="name" style="<?php echo esc_attr( $style ); ?>" onclick="return false;" href="<?php echo esc_url( home_url( '/' ) ); ?>"><?php bloginfo( 'name' ); ?></a></h1>
		<h2 id="desc" class="displaying-header-text" style="<?php echo esc_attr( $style ); ?>"><?php bloginfo( 'description' ); ?></h2>
		<?php
		$header_image = get_header_image();
		if ( ! empty( $header_image ) ) :
			?>
			<img src="<?php echo esc_url( $header_image ); ?>" class="header-image" width="<?php echo esc_attr( get_custom_header()->width ); ?>" height="<?php echo esc_attr( get_custom_header()->height ); ?>" alt="" />
		<?php endif; ?>
	</div>
	<?php
}
