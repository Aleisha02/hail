package org.broadinstitute.k3.methods

import org.broadinstitute.k3.SparkSuite
import org.broadinstitute.k3.driver.VariantQC
import org.testng.annotations.Test

class AlleleBalanceSuite extends SparkSuite {
  @Test def test() {
    val vds = LoadVCF(sc, "src/test/resources/ab_test.vcf")
    val results = VariantQC.results(vds, Array(AlleleDepthPerVariant), Array(AlleleBalancePerVariant))
      .map { case (v, a) => (v.start, (a(0).asInstanceOf[(Int, Int)], a(1).asInstanceOf[Double])) }
      .collect()
      .toMap
    val expected = Map(1 ->((0, 0), 1.0),
      2 ->((0, 0), 1.0),
      3 ->((16, 16), 1.0),
      4 ->((5, 8), 0.423950))
    for ((k, v) <- expected) {
      val u = results(k)

      assert(u._1 == v._1)
      assert((u._2 - v._2).abs < 1e-6)
    }
  }
}
