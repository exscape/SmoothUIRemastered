package smoothui.gwent
{
	import flash.display.MovieClip;
	import flash.events.Event;
	import flash.utils.Dictionary;
	import flash.utils.getDefinitionByName;
	import flash.utils.getQualifiedClassName;
	import flash.utils.getTimer;

	/**
	 * Smooth UI's Gwent speed options: faster (or instant) card moves, a shorter look at the card the
	 * opponent plays, skipped card effects, and shorter banners ("Your turn" and the like). The options
	 * come from the game's settings menu; the mod's WitcherScript sends them when a match starts (see
	 * smoothui_gwent.ws).
	 *
	 * The patcher adds this class to gwint_game.redswf as a DoABC tag of its own, and changes eight of
	 * the file's methods to use it (see gwent_speed_options.py):
	 * - GwintGameMenu.configUI calls install(this), before it tells the game script that it is ready
	 * - CardTweenManager.calcTweenDuration divides by cardSpeed rather than its MOVE_TWEEN_SPEED (2000)
	 * - CardTweenManager.tweenTo calls onCardTween(the move it starts), or onCardAtTarget(card, callback) if
	 *   the card is where it is to go already (and it makes no move)
	 * - CardFXManager.spawnFX, playRowEffect and spawnFX_Location call onCardFX(the card effect they start)
	 * - AIPlayerController.state_begin_DelayAction waits opponentCardDelay rather than 1200 ms before the
	 *   opponent plays the card it shows on screen
	 * - AIPlayerController.state_begin_SendingCard calls onOpponentCardSent(this) after it starts moving
	 *   that card into view; with no opponent card time, the card isn't shown at all
	 *
	 * Banners keep their hold for as long as asked (longer or shorter than the game's own: the clip is
	 * stopped while it shows the banner, which doesn't move then), and then skip the rest, as clicking
	 * them does. They still fade in and out as usual. With a hold of 0, they don't show at all (no fades
	 * either). Times are in seconds, so the 60 and 120 fps builds behave the same.
	 *
	 * Card effects (animations the game waits for: see onCardFX) can be skipped, by kind. A skipped one is
	 * hidden at once, and ended on the next frame, as its timeline would end it.
	 *
	 * If the game runs Gwent matches in one ActionScript domain, this class (and its static state) is
	 * the one from the first match, so install() starts afresh every time.
	 */
	public class GwentSpeed
	{
		public static const CONFIG_KEY:String = "smoothui.gwent.config";
		public static const DEFAULT_CARD_SPEED:Number = 2000;   // px per second, as in CardTweenManager
		public static const DEFAULT_OPPONENT_CARD_DELAY:Number = 1200;     // ms, as in AIPlayerController
		private static const MIN_CARD_SPEED_PERCENT:Number = 50;
		private static const MAX_CARD_SPEED_PERCENT:Number = 1000;
		// "Instant" card moves (a cardSpeed option of 0): this fast, a move takes no time, but it isn't 0, so that
		// GTween ends it on its next tick (the next frame, without a frame in between), as usual. With a duration of
		// 0, it would end within its constructor, before CardTweenManager even records the move, and call back the
		// game's code in an order it isn't written for (and CardTweenManager.flipTo would get stuck).
		private static const INSTANT_CARD_SPEED:Number = 1e9;
		// Timer delays below this are not something to rely on
		private static const MIN_TIMER_DELAY:Number = 1;

		// Card moves, in px per second (read by the patched CardTweenManager.calcTweenDuration)
		public static var cardSpeed:Number = DEFAULT_CARD_SPEED;

		// How long the opponent shows the card it plays, in ms (read by the patched
		// AIPlayerController.state_begin_DelayAction)
		public static var opponentCardDelay:Number = DEFAULT_OPPONENT_CARD_DELAY;

		// The opponent card time is 0: the card the opponent plays goes straight from its hand to the board
		public static var opponentCardHidden:Boolean = false;

		// How long banners stay fully shown, in seconds; 0: not shown at all; below 0: as long as the game shows
		// them (2.3 s). Which banners each is for: see bannerHold.
		public static var turnBannerHold:Number = -1;
		public static var firstBannerHold:Number = -1;
		public static var resultBannerHold:Number = -1;
		public static var otherBannerHold:Number = -1;
		// Do card effects play (or are they skipped)? See onCardFX for which is which.
		public static var landingEffects:Boolean = true;
		public static var cardEffects:Boolean = true;
		public static var weatherIntros:Boolean = true;

		// The banners (labels of W3MessageQueue.mcImages) that turnBannerHold and resultBannerHold are for
		private static const TURN_BANNERS:Object = { your_turn: true, Opponents_turn: true };
		private static const RESULT_BANNERS:Object = { battle_won: true, battle_lost: true, battle_draw: true };

		// The banner of a player who passed: the player's own goes with the turn banners, the opponent's with the
		// round results (without it, the game would seem stuck while the player plays on alone). It shows while
		// the game flow's currentPlayer is the one who passed (GwintGameFlowController.state_begin_ChangingPlayer).
		private static const PASSED_BANNER:String = "passed";
		private static const PLAYER_1:int = 0;    // CardManager.PLAYER_1, the player (rather than the opponent)

		// The game flow's state while the banners saying who goes first show (after the coin toss, or a
		// Scoia'tael player's choice), which firstBannerHold is for. The state can't change while a banner
		// shows (GwintGameFlowController.shouldDisallowStateChangeFunc).
		private static const FIRST_BANNER_STATE:String = "CoinToss";

		// The game flow's states with banners that have no image of their own (a leader cancelling the other's
		// ability, a match restarting), so that mcImages still shows an earlier banner's image (at first,
		// battle_won): these are for otherBannerHold. (So is a Scoia'tael player's choice, in FIRST_BANNER_STATE.)
		private static const IMAGELESS_BANNER_STATES:Object = { SpawnLeaders: true, Reset: true };
		// Card effects (their classes, as CardFXManager's settings in GwintGameMenu name them): a unit card landing on
		// the board (for landingEffects), and a weather card's (or a leader's clear weather) intro, before its
		// ongoing effect on the rows (for weatherIntros; the ongoing effect isn't one of these). Every other one,
		// from special cards and abilities (Decoy, Horn, Scorch, Spy, Medic, Muster...), is for cardEffects.
		private static const LANDING_FX:Object = { CardFXDeploy: true, CardFXHero: true };
		private static const WEATHER_FX:Object = { CardFXFrost: true, CardFXFog: true, CardFXRain: true, CardFXClearSky: true };
		// W3MessageQueue (mcStateMessage) labels: idle, fading in, fully shown (then Hiding: fading out)
		private static const IDLE_LABEL:String = "Idle";
		private static const SHOW_LABEL:String = "Show";
		private static const SHOWING_LABEL:String = "Showing";

		private static var queue:MovieClip = null;     // the menu's W3MessageQueue (mcStateMessage)
		private static var flow:Object = null;         // the menu's GwintGameFlowController
		private static var lastLabel:String = null;    // the queue's label on the frame before
		private static var hold:Number = -1;           // the current banner's hold (see bannerHold)
		private static var shownSince:int = -1;        // getTimer() when the current banner was fully shown
		// card -> the last instant move that onCardTween put it in place for (weak keys)
		private static var placedMoves:Dictionary = new Dictionary(true);

		/** menu: the GwintGameMenu */
		public static function install(menu:Object):void
		{
			cardSpeed = DEFAULT_CARD_SPEED;
			opponentCardDelay = DEFAULT_OPPONENT_CARD_DELAY;
			opponentCardHidden = false;
			turnBannerHold = -1;
			firstBannerHold = -1;
			resultBannerHold = -1;
			otherBannerHold = -1;
			landingEffects = true;
			cardEffects = true;
			weatherIntros = true;
			lastLabel = null;
			hold = -1;
			shownSince = -1;
			flow = null;
			placedMoves = new Dictionary(true);
			// An error here would stop the menu from starting the match
			try
			{
				var gameEvent:Class = getDefinitionByName("red.core.events.GameEvent") as Class;
				menu.dispatchEvent(new gameEvent(gameEvent.REGISTER, CONFIG_KEY, [onConfig]));
				flow = menu.gameFlowController;
				if (queue != null)
					queue.removeEventListener(Event.ENTER_FRAME, onQueueFrame);
				queue = menu.mcMessageQueue as MovieClip;
				if (queue != null)
					queue.addEventListener(Event.ENTER_FRAME, onQueueFrame);
			}
			catch (error:Error)
			{
				trace("GFX [SmoothUI] GwentSpeed.install failed: " + error);
			}
		}

		/** tween: the GTween that CardTweenManager.tweenTo just made to move a card */
		public static function onCardTween(tween:Object):void
		{
			if (cardSpeed != INSTANT_CARD_SPEED || tween == null)
				return;
			// Put the card where it goes now: the move only ends on GTween's next tick, and the frame may be drawn
			// before that (with the card in its new state, e.g. large, where it was). The tween takes its start
			// from the card when it first updates, so it then goes nowhere, and ends (and calls back) as usual.
			try
			{
				var card:Object = tween.target;
				card.x = tween.getValue("x");
				card.y = tween.getValue("y");
				placedMoves[card] = tween;
			}
			catch (error:Error)
			{
				trace("GFX [SmoothUI] GwentSpeed.onCardTween failed: " + error);
			}
		}

		/** card, finishCallback: what CardTweenManager.tweenTo was asked to move (and call when the move ends),
		 * when the card is where it is to go already, so that tweenTo makes no move (and calls nothing) */
		public static function onCardAtTarget(card:Object, finishCallback:Function):void
		{
			// With instant moves, the card can be there because onCardTween put it there, for a move that hasn't
			// ended yet, and that tweenTo has just stopped (without calling back) before it gave up. That happens
			// if a row places its cards twice before GTween's next tick, with as many cards. The game expects a
			// stopped move to be replaced by one that calls back: without the call, a card that was moving onto
			// the board never lands (no landing effect, and it neither gets nor gives tight bond, morale or horn
			// boosts). So the stopped move ends after all (on GTween's next tick, as it would have), and calls
			// what the new one would have. (Without instant moves, a card only gets where it goes as its move ends.)
			// With no finishCallback, it ends without a call, as the new move would: left stopped, it would stay
			// there to resume, and a later call for a card that got there long since would call back.
			if (card == null)
				return;
			try
			{
				var tween:Object = placedMoves[card];
				if (tween == null || !tween.paused || tween.position >= tween.duration
						|| tween.getValue("x") != card.x || tween.getValue("y") != card.y)
					return;
				tween.onComplete = finishCallback;
				tween.paused = false;
			}
			catch (error:Error)
			{
				trace("GFX [SmoothUI] GwentSpeed.onCardAtTarget failed: " + error);
			}
		}

		/** fx: the CardFX that CardFXManager has just started (spawnFX, playRowEffect, spawnFX_Location) */
		public static function onCardFX(fx:MovieClip):void
		{
			if (fx == null)
				return;
			var name:String = getQualifiedClassName(fx);
			if (LANDING_FX[name] == true ? landingEffects : WEATHER_FX[name] == true ? weatherIntros : cardEffects)
				return;
			// Skip it: hidden now, ended on the next frame. Not now: its callbacks are set by whoever started it,
			// after it started (e.g. Mardroeme's and the berserkers' midFXPointCallback, which transforms cards).
			// (Its sound has played already: CardFXManager plays it before it starts the effect.)
			fx.stop();
			fx.visible = false;
			fx.addEventListener(Event.ENTER_FRAME, onSkippedFXFrame);
		}

		private static function onSkippedFXFrame(event:Event):void
		{
			var fx:Object = event.currentTarget;
			fx.removeEventListener(Event.ENTER_FRAME, onSkippedFXFrame);
			try
			{
				// What its timeline does, in order: midFXPoint (for some) on a frame halfway, fxEnded on its last
				fx.midFXPoint();
				fx.fxEnded();
			}
			catch (error:Error)
			{
				trace("GFX [SmoothUI] GwentSpeed.onSkippedFXFrame failed: " + error);
			}
		}

		/** controller: the AIPlayerController, which has just started moving the card it plays into view */
		public static function onOpponentCardSent(controller:Object):void
		{
			if (!opponentCardHidden)
				return;
			try
			{
				var card:Object = controller.transactionCard;
				if (card == null)
					return;
				// The move just started, so the card is still where it was stored: moving it there stops the move
				var tweens:Object = getDefinitionByName("red.game.witcher3.menus.gwint.CardTweenManager");
				tweens.getInstance().restorePosition(card, true);
				// Shown on the board's scale, as it will be, rather than large (it would peek into view)
				card.cardState = getDefinitionByName("red.game.witcher3.menus.gwint.CardSlot").STATE_BOARD;
				// The card's description, which fades in next to it
				var tooltip:Object = controller.boardRenderer != null ? controller.boardRenderer.mcTransactionTooltip : null;
				if (tooltip != null)
				{
					getDefinitionByName("com.gskinner.motion.GTweener").removeTweens(tooltip);
					tooltip.alpha = 0;
					tooltip.visible = false;
				}
			}
			catch (error:Error)
			{
				trace("GFX [SmoothUI] GwentSpeed.onOpponentCardSent failed: " + error);
			}
		}

		/** The options from the game script: cardSpeed (percent; 0: instant); opponentCardDelay, turnBannerHold,
		 * firstBannerHold, resultBannerHold and otherBannerHold (seconds; below 0: the game's own);
		 * landingEffects, cardEffects and weatherIntros (0: skipped; otherwise played) */
		public static function onConfig(config:Object):void
		{
			if (config == null)
				return;
			// A value far off (a bug in the script) would make the game unplayable
			var percent:Number = Number(config.cardSpeed);
			if (percent == 0)
				cardSpeed = INSTANT_CARD_SPEED;
			else
				cardSpeed = percent >= MIN_CARD_SPEED_PERCENT && percent <= MAX_CARD_SPEED_PERCENT
					? DEFAULT_CARD_SPEED * percent / 100 : DEFAULT_CARD_SPEED;
			var delay:Number = secondsOption(config.opponentCardDelay);
			opponentCardDelay = delay >= 0 ? Math.max(delay * 1000, MIN_TIMER_DELAY) : DEFAULT_OPPONENT_CARD_DELAY;
			opponentCardHidden = delay == 0;
			turnBannerHold = secondsOption(config.turnBannerHold);
			firstBannerHold = secondsOption(config.firstBannerHold);
			resultBannerHold = secondsOption(config.resultBannerHold);
			otherBannerHold = secondsOption(config.otherBannerHold);
			landingEffects = playOption(config.landingEffects);
			cardEffects = playOption(config.cardEffects);
			weatherIntros = playOption(config.weatherIntros);
			trace("GFX [SmoothUI] Gwent options: cardSpeed " + cardSpeed + ", opponentCardDelay " + opponentCardDelay
				+ ", opponentCardHidden " + opponentCardHidden
				+ ", turnBannerHold " + turnBannerHold + ", firstBannerHold " + firstBannerHold
				+ ", resultBannerHold " + resultBannerHold + ", otherBannerHold " + otherBannerHold
				+ ", landingEffects " + landingEffects + ", cardEffects " + cardEffects + ", weatherIntros " + weatherIntros);
		}

		/** An option to play (rather than skip) something: anything but 0, or missing */
		private static function playOption(value:*):Boolean
		{
			return value == undefined || Number(value) != 0;
		}

		/** An option in seconds; -1 (the game's own) if it is missing */
		private static function secondsOption(value:*):Number
		{
			var seconds:Number = Number(value);
			return isNaN(seconds) ? -1 : seconds;
		}

		private static function onQueueFrame(event:Event):void
		{
			var label:String = queue.currentLabel;
			// A banner starts with its fade-in (and its image and the game's state are set by then)
			if (label == SHOW_LABEL && lastLabel != SHOW_LABEL)
			{
				hold = bannerHold();
				shownSince = -1;
			}
			lastLabel = label;
			if (!queue._showingMessage || label == IDLE_LABEL)
				return;
			if (hold == 0)
			{
				// Not shown at all (by now it has at most begun to fade in, from nothing)
				endBanner();
			}
			else if (hold > 0 && label == SHOWING_LABEL)
			{
				// The time decides when it goes (the clip's own hold may be shorter)
				if (shownSince < 0)
					shownSince = getTimer();
				queue.stop();
				if (getTimer() - shownSince >= hold * 1000)
					queue.trySkipMessage();
			}
		}

		/** End the banner at once: what the last frame of its fade-out does */
		private static function endBanner():void
		{
			hold = -1;
			lastLabel = IDLE_LABEL;
			queue.gotoAndPlay(IDLE_LABEL);
			queue.OnShowMessageEnded();
		}

		/** The hold for the banner showing now */
		private static function bannerHold():Number
		{
			var state:String = flow != null && flow.stateMachine != null ? flow.stateMachine.currentState : null;
			if (state == FIRST_BANNER_STATE)
				return firstBannerHold;
			if (IMAGELESS_BANNER_STATES[state] == true)
				return otherBannerHold;
			var images:MovieClip = queue.mcImages as MovieClip;
			var image:String = images != null ? images.currentLabel : null;
			if (TURN_BANNERS[image] == true)
				return turnBannerHold;
			if (RESULT_BANNERS[image] == true)
				return resultBannerHold;
			if (image == PASSED_BANNER)
				return flow != null && flow.currentPlayer == PLAYER_1 ? turnBannerHold : resultBannerHold;
			// Round start, and faction abilities
			return otherBannerHold;
		}
	}
}
