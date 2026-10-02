# branding/boot.svg as images: the Jetson UEFI boot logo and the installer menus.
{
  imagemagick,
  librsvg,
  runCommand,
}:
runCommand "omarchy-branding"
  {
    nativeBuildInputs = [
      imagemagick
      librsvg
    ];
  }
  ''
    mkdir $out
    rsvg-convert ${../../branding/boot.svg} -o $out/boot.png
    magick $out/boot.png -resize 640x480 -background black -gravity center -extent 640x480 $out/splash-640x480.png
  ''
