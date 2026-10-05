{
  description = "turnstile: a mechanical gate on the push path";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: {
        default = pkgs.stdenvNoCC.mkDerivation {
          pname = "turnstile";
          version = "0.2.0-prototype";
          src = self;

          buildInputs = [
            pkgs.bash
            pkgs.python3
          ];

          dontConfigure = true;
          dontBuild = true;

          installPhase =
            let
              path = pkgs.lib.makeBinPath [
                pkgs.bash
                pkgs.git
                pkgs.python3
                pkgs.coreutils
                pkgs.gawk
                pkgs.gnused
                pkgs.findutils
              ];
            in
            ''
              runHook preInstall

              home=$out/share/turnstile
              mkdir -p $home $out/bin
              cp -r bin hooks claude modules $home/
              chmod -R u+w $home

              substituteInPlace $home/bin/turnstile \
                --replace-fail 'set -uo pipefail' 'set -uo pipefail
              export PATH="${path}:$PATH"'
              substituteInPlace $home/hooks/dispatch \
                --replace-fail 'set -uo pipefail' 'set -uo pipefail
              export PATH="${path}:$PATH"'
              substituteInPlace $home/bin/turnstile-ai \
                --replace-fail $'import time\n' $'import time\n\nos.environ["PATH"] = "${path}:" + os.environ.get("PATH", "")\n'

              ln -s $home/bin/turnstile $out/bin/turnstile
              ln -s $home/bin/turnstile-ai $out/bin/turnstile-ai

              runHook postInstall
            '';

          meta = {
            description = "A mechanical gate on the push path";
            homepage = "https://github.com/a-grasso/turnstile";
            mainProgram = "turnstile";
            platforms = systems;
          };
        };
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/turnstile";
        };
      });

      checks = forAllSystems (pkgs: {
        default =
          pkgs.runCommand "turnstile-tests"
            {
              nativeBuildInputs = [
                pkgs.git
                pkgs.python3
              ];
            }
            ''
              export HOME=$TMPDIR
              mkdir -p $TMPDIR/turnstile
              cp -r ${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/share/turnstile/. $TMPDIR/turnstile/
              chmod -R u+w $TMPDIR/turnstile
              cp ${self}/README.md $TMPDIR/turnstile/README.md
              cp -r ${self}/tests $TMPDIR/turnstile/tests
              chmod -R u+w $TMPDIR/turnstile/tests
              python3 $TMPDIR/turnstile/tests/test_turnstile.py
              touch $out
            '';
      });
    };
}
