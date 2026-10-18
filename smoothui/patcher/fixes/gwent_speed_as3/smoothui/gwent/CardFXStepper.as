package smoothui.gwent
{
	import flash.display.DisplayObjectContainer;
	import flash.display.MovieClip;
	import flash.events.Event;
	import flash.utils.getQualifiedClassName;

	/**
	 * Plays a card effect (a CardFX: see GwentSpeed.onCardFX) faster than the game does. On every frame, on top
	 * of the frame the game plays, it moves the effect on by speed - 1 frames (carrying a fraction over to the
	 * next frame).
	 *
	 * Moving a clip with gotoAndPlay doesn't run the frame script of the frame it moves to (in Scaleform), so
	 * this never moves the effect onto a frame with a script, nor away from one: the game plays those frames,
	 * and runs their scripts (on the last one, which tells CardFXManager that the effect is over; Morph's
	 * midpoint, where Mardroeme and the berserkers change their cards). That costs a frame or two of speed.
	 * Which frames have scripts (after the rescale, which moves them) comes from the patcher (SCRIPT_FRAMES).
	 *
	 * An effect is built from clips with timelines of their own, nested in each other, so all of them move on
	 * together, one frame at a time, as the game plays them:
	 * - Clips with frame scripts (the class Flash makes for a symbol with frame scripts) stop where their
	 *   scripts stop them (the patcher leaves out effects with clips whose scripts do more than that).
	 * - Other clips loop.
	 * - A clip that a step adds starts on its first frame, as when the game plays it.
	 *
	 * Should anything fail, the effect plays on at the game's speed.
	 */
	public class CardFXStepper
	{
		// Filled in by the patcher (gwent_speed_options.py): the frames with frame scripts of each card effect,
		// and of the clips with scripts in it, after the rescale: "class=frames;class=frames...", with class
		// names as getQualifiedClassName gives them, and 1-based frame numbers, s before those whose script
		// only calls stop()
		private static const SCRIPT_FRAMES:String = "@SMOOTHUI_FX_SCRIPT_FRAMES@";
		// class name -> its frames with scripts: frame number -> true if the script only stops the clip
		private static var scriptFrames:Object = null;

		private var fx:MovieClip;
		private var fxFrames:Object;               // the effect's frames with scripts (see scriptFrames)
		private var extra:Number;                  // frames to add on every frame: speed - 1
		private var owed:Number = 0;               // the fraction of a frame carried over

		/** Can effects of this class (getQualifiedClassName) be sped up? (Do we know their frame scripts?) */
		public static function canSpeedUp(className:String):Boolean
		{
			return framesOf(className) != null;
		}

		/** fx: the CardFX, just started (see canSpeedUp); speed: above 1 */
		public function CardFXStepper(fx:MovieClip, speed:Number)
		{
			this.fx = fx;
			this.fxFrames = framesOf(getQualifiedClassName(fx));
			this.extra = speed - 1;
			if (fxFrames != null)
				fx.addEventListener(Event.ENTER_FRAME, onFrame);
		}

		private function onFrame(event:Event):void
		{
			// Removed (CardFXManager removes an effect as it ends)
			if (fx.parent == null)
			{
				finish();
				return;
			}
			try
			{
				owed += extra;
				var steps:int = int(owed);
				owed -= steps;
				for (var i:int = 0; i < steps && canStep(); i++)
					step();
			}
			catch (error:Error)
			{
				trace("GFX [SmoothUI] CardFXStepper failed: " + error);
				finish();
			}
		}

		/** Can the effect move on by a frame? Not onto a frame with a script, nor away from one (whose script
		 * may not have run yet), nor past its last frame. */
		private function canStep():Boolean
		{
			var frame:int = fx.currentFrame;
			return frame < fx.totalFrames && !(frame in fxFrames) && !((frame + 1) in fxFrames);
		}

		/** Moves the effect, and every clip in it, on by a frame */
		private function step():void
		{
			// The clips there are now, deepest first, so that the effect itself moves last
			var clips:Array = [];
			collect(fx, clips);
			for each (var clip:MovieClip in clips)
			{
				var frame:int = clip.currentFrame;
				if (clip == fx)
				{
					clip.gotoAndPlay(frame + 1);
					continue;
				}
				var frames:Object = framesOf(getQualifiedClassName(clip));
				// Stopped there by its script
				if (frames != null && frames[frame] === true)
					continue;
				var next:int = frame < clip.totalFrames ? frame + 1 : 1;
				// What its script there does
				if (frames != null && frames[next] === true)
					clip.gotoAndStop(next);
				else
					clip.gotoAndPlay(next);
			}
		}

		private static function collect(container:DisplayObjectContainer, clips:Array):void
		{
			for (var i:int = 0; i < container.numChildren; i++)
			{
				var child:DisplayObjectContainer = container.getChildAt(i) as DisplayObjectContainer;
				if (child != null)
					collect(child, clips);
			}
			var clip:MovieClip = container as MovieClip;
			if (clip != null && clip.totalFrames > 1)
				clips.push(clip);
		}

		private function finish():void
		{
			fx.removeEventListener(Event.ENTER_FRAME, onFrame);
		}

		/** A class's frames with scripts (see scriptFrames); null if it has none, or we don't know them */
		private static function framesOf(className:String):Object
		{
			if (scriptFrames == null)
			{
				scriptFrames = {};
				for each (var entry:String in SCRIPT_FRAMES.split(";"))
				{
					var parts:Array = entry.split("=");
					if (parts.length != 2)
						continue;
					var frames:Object = {};
					for each (var frame:String in String(parts[1]).split(","))
					{
						var stops:Boolean = frame.charAt(0) == "s";
						frames[int(stops ? frame.substr(1) : frame)] = stops;
					}
					scriptFrames[parts[0]] = frames;
				}
			}
			return scriptFrames.hasOwnProperty(className) ? scriptFrames[className] : null;
		}
	}
}
